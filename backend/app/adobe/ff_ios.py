"""Minimal Firefly iOS credential broker.

Long-lived device grants are encrypted by the account service. This module only
returns them to its caller; short access tokens remain in memory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from app.adobe.http import AdobeProxyRequired, is_transient_connect, session_proxies

FF_IOS_CLIENT_ID = "FF-iOS"
FF_IOS_UA = "Firefly/26.15.0 (AdobeCreativeSDK 11.4.2438;Apple;iPhone;iOS;26.6.1)"
FF_IOS_SCOPE = "creative_sdk,AdobeID,openid,creative_cloud,firefly_api"
FF_IOS_REDIRECT = "com.adobe.firefly.ios://login.complete"
AUTHORIZE_URLS = (
    "https://adobeid-na1.services.adobe.com/ims/authorize/v3",
    "https://ims-na1.adobelogin.com/ims/authorize/v3",
)
TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v4"
AUTHORIZE_DEVICE_URLS = (
    "https://adobeid-na1.services.adobe.com/ims/authorize/v2",
    "https://ims-na1.adobelogin.com/ims/authorize/v2",
)
SSO_URL = "https://adobeid-na1.services.adobe.com/ims/check/v6/token"


class FFIOSAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class FFIOSCredentials:
    access_token: str
    account_id: str
    expires_in: int
    device_token: str
    device_id: str


def _claims(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _account_id(token: str) -> str:
    claims = _claims(token)
    return str(claims.get("user_id") or claims.get("aa_id") or claims.get("sub") or "")


def _client_id(token: str) -> str:
    claims = _claims(token)
    return str(claims.get("client_id") or claims.get("clientId") or "")


def _device_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")


def _classify(status: int, body: str) -> FFIOSAuthError:
    lowered = body.lower()
    if status == 429:
        return FFIOSAuthError("FF-iOS IMS rate limited", code="rate_limited", status_code=429)
    if status >= 500:
        return FFIOSAuthError("FF-iOS IMS unavailable", code="upstream_error", status_code=status)
    if status in {401, 403} or "invalid_credentials" in lowered or "invalid_grant" in lowered:
        return FFIOSAuthError("FF-iOS credential rejected", code="auth_invalid", status_code=status)
    return FFIOSAuthError("FF-iOS authorization failed", code="auth_failed", status_code=status)


def _token_payload(response: requests.Response) -> dict[str, object]:
    if response.status_code != 200:
        raise _classify(response.status_code, response.text[:500])
    try:
        payload = json.loads(response.content)
    except Exception as exc:
        raise FFIOSAuthError("FF-iOS token response was invalid", code="upstream_error") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise FFIOSAuthError("FF-iOS token response had no access token", code="auth_invalid")
    token = str(payload["access_token"])
    if _client_id(token) != FF_IOS_CLIENT_ID:
        raise FFIOSAuthError("FF-iOS returned the wrong token identity", code="identity_rejected")
    return payload


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _payload(response: requests.Response) -> dict[str, object]:
    if response.status_code >= 400:
        raise _classify(response.status_code, response.text[:500])
    try:
        value = json.loads(response.content)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _device_fields(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    fields: dict[str, str] = {}
    for key in (
        "access_token",
        "device_token",
        "device_id",
        "code",
        "userId",
        "user_id",
    ):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            fields[key] = item.strip()
    nested = value.get("token")
    if isinstance(nested, dict):
        for key, item in _device_fields(nested).items():
            fields.setdefault(key, item)
    return fields


def _redirect_fields(location: str) -> dict[str, str]:
    parsed = urlparse(location or "")
    fields: dict[str, str] = {}
    for bucket in (parse_qs(parsed.query), parse_qs(parsed.fragment)):
        for key in ("access_token", "device_token", "device_id", "code"):
            values = bucket.get(key) or []
            if values and values[0]:
                fields[key] = str(values[0]).strip()
    return fields


def _ready_credentials(
    fields: dict[str, str], *, fallback_device_id: str, expires_in: int = 0
) -> FFIOSCredentials | None:
    access = fields.get("access_token", "")
    device_token = fields.get("device_token", "")
    if not access or not device_token or _client_id(access) != FF_IOS_CLIENT_ID:
        return None
    return FFIOSCredentials(
        access_token=access,
        account_id=_account_id(access),
        expires_in=expires_in,
        device_token=device_token,
        device_id=fields.get("device_id") or fallback_device_id,
    )


def refresh(
    *, device_token: str, device_id: str, proxy_url: str, impersonate: str
) -> FFIOSCredentials:
    try:
        proxies = session_proxies(proxy_url)
    except AdobeProxyRequired as exc:
        raise FFIOSAuthError("FF-iOS requires a proxy", code="proxy_required") from exc
    try:
        with requests.Session(impersonate=impersonate, proxies=proxies) as session:
            response = session.post(
                TOKEN_URL,
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "user-agent": FF_IOS_UA,
                    "x-api-key": FF_IOS_CLIENT_ID,
                    "x-ims-clientid": FF_IOS_CLIENT_ID,
                },
                data=urlencode(
                    {
                        "grant_type": "device",
                        "client_id": FF_IOS_CLIENT_ID,
                        "device_id": device_id,
                        "device_token": device_token,
                        "scope": FF_IOS_SCOPE,
                    }
                ),
                timeout=60,
            )
    except Exception as exc:
        if is_transient_connect(exc):
            raise FFIOSAuthError("FF-iOS connection dropped", code="proxy_tls") from exc
        raise
    payload = _token_payload(response)
    access = str(payload["access_token"])
    return FFIOSCredentials(
        access_token=access,
        account_id=_account_id(access),
        expires_in=_as_int(payload.get("expires_in")),
        device_token=str(payload.get("device_token") or device_token),
        device_id=device_id,
    )


def mint(
    *, cookie: str, seed: str, proxy_url: str, impersonate: str
) -> FFIOSCredentials:
    """Authorize once with the account Cookie, then persist only the device grant."""
    try:
        proxies = session_proxies(proxy_url)
    except AdobeProxyRequired as exc:
        raise FFIOSAuthError("FF-iOS requires a proxy", code="proxy_required") from exc
    device_id = _device_id(seed)
    verifier = _verifier()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    query = urlencode(
        {
            "client_id": FF_IOS_CLIENT_ID,
            "response_type": "code",
            "scope": FF_IOS_SCOPE,
            "redirect_uri": FF_IOS_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "device_id": device_id,
            "locale": "en_US",
        }
    )
    headers = {
        "accept": "application/json",
        "cookie": cookie,
        "user-agent": FF_IOS_UA,
        "x-api-key": FF_IOS_CLIENT_ID,
        "x-ims-clientid": FF_IOS_CLIENT_ID,
    }
    code = ""
    try:
        with requests.Session(impersonate=impersonate, proxies=proxies) as session:
            ims_headers = {
                **headers,
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            }
            for base in AUTHORIZE_URLS:
                response = session.get(
                    f"{base}?{query}",
                    headers=headers,
                    allow_redirects=False,
                    timeout=60,
                )
                location = response.headers.get("location", "")
                values = parse_qs(urlparse(location).query)
                code = str((values.get("code") or [""])[0])
                if code:
                    break
                if response.status_code >= 400:
                    raise _classify(response.status_code, response.text[:500])
            if code:
                token_response = session.post(
                    TOKEN_URL,
                    headers=ims_headers,
                    data=urlencode(
                        {
                            "grant_type": "authorization_code",
                            "client_id": FF_IOS_CLIENT_ID,
                            "code": code,
                            "code_verifier": verifier,
                            "redirect_uri": FF_IOS_REDIRECT,
                            "device_id": device_id,
                        }
                    ),
                    timeout=60,
                )
                token_payload = _token_payload(token_response)
                ready = _ready_credentials(
                    _device_fields(token_payload),
                    fallback_device_id=device_id,
                    expires_in=_as_int(token_payload.get("expires_in")),
                )
                if ready is not None:
                    return ready

            # Some valid Adobe sessions do not issue an authorization code. The
            # native client falls back to the device authorize and SSO flows.
            device_query = urlencode(
                {
                    "client_id": FF_IOS_CLIENT_ID,
                    "response_type": "device",
                    "device_id": device_id,
                    "device_name": "iPhone",
                    "scope": FF_IOS_SCOPE,
                    "redirect_uri": FF_IOS_REDIRECT,
                    "prompt": "none",
                    "locale": "en_US",
                }
            )
            for base in AUTHORIZE_DEVICE_URLS:
                response = session.get(
                    f"{base}?{device_query}",
                    headers=headers,
                    allow_redirects=False,
                    timeout=60,
                )
                if response.status_code >= 400:
                    continue
                fields = {
                    **_device_fields(_payload(response)),
                    **_redirect_fields(response.headers.get("location", "")),
                }
                ready = _ready_credentials(fields, fallback_device_id=device_id)
                if ready is not None:
                    return ready
                if fields.get("device_token"):
                    return refresh(
                        device_token=fields["device_token"],
                        device_id=fields.get("device_id") or device_id,
                        proxy_url=proxy_url,
                        impersonate=impersonate,
                    )

            sso_response = session.post(
                SSO_URL,
                headers=ims_headers,
                data=urlencode(
                    {
                        "client_id": FF_IOS_CLIENT_ID,
                        "scope": FF_IOS_SCOPE,
                        "guest_allowed": "true",
                        "credential": "sso",
                        "device_id": device_id,
                        "device_name": "iPhone",
                    }
                ),
                timeout=60,
            )
            sso_payload = _payload(sso_response)
            sso_fields = _device_fields(sso_payload)
            ready = _ready_credentials(
                sso_fields,
                fallback_device_id=device_id,
                expires_in=_as_int(sso_payload.get("expires_in")),
            )
            if ready is not None:
                return ready
            if sso_fields.get("device_token"):
                return refresh(
                    device_token=sso_fields["device_token"],
                    device_id=sso_fields.get("device_id") or device_id,
                    proxy_url=proxy_url,
                    impersonate=impersonate,
                )
    except FFIOSAuthError:
        raise
    except Exception as exc:
        if is_transient_connect(exc):
            raise FFIOSAuthError("FF-iOS connection dropped", code="proxy_tls") from exc
        raise
    raise FFIOSAuthError(
        "FF-iOS authorization produced no device grant", code="identity_rejected"
    )


def submit_headers(access_token: str, account_id: str, prompt: str) -> dict[str, str]:
    headers = poll_headers(access_token)
    if account_id:
        headers["x-nonce"] = hashlib.sha256(
            f"{account_id}-{prompt.strip()[:256]}".encode()
        ).hexdigest()
    return headers


def poll_headers(access_token: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {access_token}",
        "x-api-key": FF_IOS_CLIENT_ID,
        "x-ims-clientid": FF_IOS_CLIENT_ID,
        "user-agent": FF_IOS_UA,
        "accept": "*/*",
    }


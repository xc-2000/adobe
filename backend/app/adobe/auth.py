from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests

TOKEN_URL = (
    "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
    "?jslVersion=v2-v0.48.0-1-g1e322cb"
)
CLIO_SCOPE = ",".join(
    (
        "AdobeID",
        "firefly_api",
        "openid",
        "pps.read",
        "pps.write",
        "additional_info.projectedProductContext",
        "additional_info.ownerOrg",
        "uds_read",
        "uds_write",
        "ab.manage",
        "read_organizations",
        "additional_info.roles",
        "account_cluster.read",
        "creative_production",
        "tk_platform",
        "tk_platform_sync",
        "profile",
    )
)


class AdobeAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdobeCredentials:
    access_token: str = field(repr=False)
    account_id: str
    expires_in: int


def _decode_jwt(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise AdobeAuthError("Adobe returned a non-JWT access token")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise AdobeAuthError("Adobe returned an invalid JWT") from exc
    if not isinstance(payload, dict):
        raise AdobeAuthError("Adobe JWT payload is invalid")
    return payload


def _post_token(
    session: requests.Session[requests.Response],
    *,
    cookie: str,
    origin: str,
    form: dict[str, str],
    user_agent: str,
) -> dict[str, Any]:
    response = session.post(
        TOKEN_URL,
        data=urlencode(form),
        headers={
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "cookie": cookie,
            "origin": origin,
            "referer": f"{origin}/",
            "user-agent": user_agent,
        },
        timeout=60,
    )
    if response.status_code != 200:
        raise AdobeAuthError(f"Adobe token exchange failed with HTTP {response.status_code}")
    payload = json.loads(response.content)
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AdobeAuthError("Adobe token exchange returned no access token")
    return payload


def exchange_cookie_for_clio(
    *,
    cookie: str,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
) -> AdobeCredentials:
    proxies: requests.ProxySpec = {"http": proxy_url, "https": proxy_url}
    with requests.Session(impersonate=impersonate, proxies=proxies) as session:
        express = _post_token(
            session,
            cookie=cookie,
            origin="https://new.express.adobe.com",
            form={
                "client_id": "projectx_webapp",
                "guest_allowed": "true",
                "scope": "AdobeID,firefly_api,openid",
            },
            user_agent=user_agent,
        )
        express_claims = _decode_jwt(str(express["access_token"]))
        account_id = str(
            express_claims.get("user_id")
            or express_claims.get("aa_id")
            or express_claims.get("sub")
            or ""
        )
        if not account_id or account_id.endswith("@GuestID"):
            raise AdobeAuthError("Adobe Cookie is expired or belongs to a guest")

        clio = _post_token(
            session,
            cookie=cookie,
            origin="https://firefly.adobe.com",
            form={
                "client_id": "clio-playground-web",
                "scope": CLIO_SCOPE,
                "user_id": account_id,
            },
            user_agent=user_agent,
        )
        access_token = str(clio["access_token"])
        claims = _decode_jwt(access_token)
        if claims.get("client_id") != "clio-playground-web":
            raise AdobeAuthError("Adobe returned a token for the wrong client")
        return AdobeCredentials(
            access_token=access_token,
            account_id=account_id,
            expires_in=int(clio.get("expires_in") or 0),
        )

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests

from app.adobe.http import AdobeProxyRequired, is_transient_connect, session_proxies
from app.adobe.trace import UpstreamTrace, parse_response_body

TOKEN_URL = "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
# Scope-exact with the Firefly web clio mint. Extra scopes mint a different grant.
CLIO_SCOPE = (
    "AdobeID,firefly_api,openid,pps.read,pps.write,"
    "additional_info.projectedProductContext,additional_info.ownerOrg,"
    "uds_read,uds_write,ab.manage,read_organizations,additional_info.roles,"
    "account_cluster.read,creative_production,profile"
)


class AdobeAuthError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "auth_failed", status_code: int = 0
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AdobeCredentials:
    access_token: str = field(repr=False)
    account_id: str
    expires_in: int
    token_kind: str = "clio"
    rotated_cookies: dict[str, str] = field(default_factory=dict, repr=False)


def identity_from_token(token: str) -> str:
    claims = _decode_jwt(token)
    account_id = str(claims.get("user_id") or claims.get("aa_id") or claims.get("sub") or "")
    return account_id


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
    trace: UpstreamTrace | None = None,
    step: str = "ims_token",
) -> dict[str, Any]:
    last: BaseException | None = None
    response: requests.Response | None = None
    for attempt in range(3):
        started = time.perf_counter()
        try:
            response = session.post(
                TOKEN_URL,
                data=urlencode(form),
                headers=_mint_headers(cookie=cookie, origin=origin, user_agent=user_agent),
                timeout=60,
            )
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            if trace is not None:
                trace.record(
                    step,
                    method="POST",
                    url=TOKEN_URL,
                    error=str(exc)[:400],
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            if is_transient_connect(exc) and attempt < 2:
                time.sleep(1.0)
                continue
            if is_transient_connect(exc):
                raise AdobeAuthError("upstream connection dropped", code="proxy_tls") from exc
            raise
    if response is None:
        if last is not None and is_transient_connect(last):
            raise AdobeAuthError("upstream connection dropped", code="proxy_tls") from last
        raise AdobeAuthError("Adobe token exchange failed") from last
    body = parse_response_body(response.content, response.text)
    if trace is not None:
        trace.record(
            step,
            method="POST",
            url=TOKEN_URL,
            status_code=response.status_code,
            body=body,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=None if response.status_code == 200 else f"HTTP {response.status_code}",
        )
    if response.status_code != 200:
        text = str(body).lower()
        if response.status_code == 429:
            raise AdobeAuthError(
                "Adobe token exchange rate limited",
                code="rate_limited",
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise AdobeAuthError(
                "Adobe token service unavailable",
                code="upstream_error",
                status_code=response.status_code,
            )
        if (
            response.status_code in {401, 403}
            or "invalid_credentials" in text
            or "invalid_grant" in text
        ):
            raise AdobeAuthError(
                "Adobe credential rejected",
                code="auth_invalid",
                status_code=response.status_code,
            )
        raise AdobeAuthError(
            f"Adobe token exchange failed with HTTP {response.status_code}",
            status_code=response.status_code,
        )
    payload = body if isinstance(body, dict) else json.loads(response.content)
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise AdobeAuthError("Adobe token exchange returned no access token")
    return payload


def exchange_cookie_for_clio(
    *,
    cookie: str,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
    trace: UpstreamTrace | None = None,
) -> AdobeCredentials:
    try:
        proxies = session_proxies(proxy_url)
    except AdobeProxyRequired as exc:
        raise AdobeAuthError("Adobe token exchange requires a proxy") from exc
    with requests.Session(impersonate=impersonate, proxies=proxies) as session:
        form = {"client_id": "clio-playground-web", "scope": CLIO_SCOPE}
        if not _jar_has_login_cookie(cookie):
            form["guest_allowed"] = "true"
        clio = _post_token(
            session,
            cookie=cookie,
            origin="https://firefly.adobe.com",
            form=form,
            user_agent=user_agent,
            trace=trace,
            step="ims_clio",
        )
        access_token = str(clio["access_token"])
        claims = _decode_jwt(access_token)
        if claims.get("client_id") != "clio-playground-web":
            raise AdobeAuthError(
                "Adobe returned a token for the wrong client", code="identity_rejected"
            )
        account_id = str(
            claims.get("user_id") or claims.get("aa_id") or claims.get("sub") or ""
        )
        if not account_id or account_id.endswith("@GuestID"):
            raise AdobeAuthError(
                "Adobe Cookie is expired or belongs to a guest", code="auth_invalid"
            )
        rotated: dict[str, str] = {}
        try:
            jar = session.cookies.get_dict()
            for name in ("ims_sid", "aux_sid"):
                value = jar.get(name)
                if value:
                    rotated[name] = str(value)
        except Exception:
            rotated = {}
        return AdobeCredentials(
            access_token=access_token,
            account_id=account_id,
            expires_in=int(clio.get("expires_in") or 0),
            token_kind="clio",
            rotated_cookies=rotated,
        )


def _mint_headers(*, cookie: str, origin: str, user_agent: str) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "origin": origin,
        "referer": f"{origin}/",
        "user-agent": user_agent,
        "Cookie": cookie,
    }
    headers.update(_chrome_client_hint_headers(user_agent))
    return headers


def _chrome_client_hint_headers(user_agent: str) -> dict[str, str]:
    match = re.search(r"Chrome/(\d+)", user_agent or "")
    version = match.group(1) if match else "148"
    lowered = (user_agent or "").lower()
    if "linux" in lowered:
        platform = "Linux"
    elif "mac os" in lowered or "macintosh" in lowered:
        platform = "macOS"
    else:
        platform = "Windows"
    return {
        "sec-ch-ua": (
            f'"Chromium";v="{version}", "Not?A_Brand";v="24", "Google Chrome";v="{version}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
    }


def _jar_has_login_cookie(jar: str) -> bool:
    """True when the Cookie header carries anything besides a bare ims_sid."""
    for pair in (jar or "").split(";"):
        name = pair.split("=", 1)[0].strip().lower()
        if name and name != "ims_sid":
            return True
    return False

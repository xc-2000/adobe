"""Upstream Firefly generative-credit snapshot.

The Firefly web app reads remaining quota from::

    GET https://firefly.adobe.io/v1/credits/balance
    Authorization: Bearer <clio AT>
    x-api-key: SunbreakWebUI1
    x-account-id: <IMS user id>

AT is minted per request from Cookie and must not be persisted. Response body is a
snapshot only: ``total.quota.{available,used,total}`` plus ``total.planCap``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curl_cffi import requests

from app.adobe.auth import AdobeAuthError
from app.adobe.http import AdobeProxyRequired, session_proxies

CREDITS_URL = "https://firefly.adobe.io/v1/credits/balance"
CREDITS_API_KEY = "SunbreakWebUI1"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class AdobeCreditsError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CreditBalance:
    remaining: int
    used: int
    total: int
    plan_cap: str
    available_until: str | None = None


def _as_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.strip():
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0


def parse_credits_payload(payload: dict[str, Any]) -> CreditBalance:
    total_info = payload.get("total")
    if not isinstance(total_info, dict):
        raise AdobeCreditsError("invalid_response", "credits payload missing total")
    quota = total_info.get("quota")
    if not isinstance(quota, dict):
        quota = {}
    remaining = _as_int(quota.get("available"))
    used = _as_int(quota.get("used"))
    total = _as_int(quota.get("total"))
    if total <= 0 and remaining + used > 0:
        total = remaining + used
    plan = str(total_info.get("planCap") or "").strip().upper()
    until_raw = total_info.get("availableUntil")
    until = str(until_raw).strip() if until_raw not in (None, "") else None
    return CreditBalance(
        remaining=remaining,
        used=used,
        total=total,
        plan_cap=plan if plan in {"HARD", "FREE"} else "",
        available_until=until,
    )


def fetch_credits_balance(
    *,
    access_token: str,
    account_id: str,
    proxy_url: str,
    user_agent: str,
    impersonate: str,
    api_key: str = CREDITS_API_KEY,
) -> CreditBalance:
    if not access_token or not account_id:
        raise AdobeCreditsError("invalid_token", "missing access token or account id")
    try:
        proxies = session_proxies(proxy_url)
    except AdobeProxyRequired as exc:
        raise AdobeCreditsError("proxy_required", "credits query requires a proxy") from exc
    with requests.Session(impersonate=impersonate, proxies=proxies) as session:
        response = session.get(
            CREDITS_URL,
            headers={
                "authorization": f"Bearer {access_token}",
                "x-api-key": api_key,
                "x-account-id": account_id,
                "accept": "application/json",
                "user-agent": user_agent,
            },
            timeout=45,
        )
    if response.status_code == 401:
        raise AdobeAuthError("Adobe credits query returned HTTP 401")
    if response.status_code != 200:
        raise AdobeCreditsError(
            "upstream_error",
            f"credits query failed with HTTP {response.status_code}",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AdobeCreditsError("invalid_response", "credits response was not JSON") from exc
    if not isinstance(payload, dict):
        raise AdobeCreditsError("invalid_response", "credits response was not an object")
    return parse_credits_payload(payload)

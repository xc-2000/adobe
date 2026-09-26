from app.adobe.auth import AdobeAuthError, exchange_cookie_for_clio
from app.adobe.credits import AdobeCreditsError, fetch_credits_balance, parse_credits_payload
from app.adobe.http import AdobeProxyRequired, require_proxy_url


def test_parse_credits_payload_reads_quota_and_plan() -> None:
    balance = parse_credits_payload(
        {
            "total": {
                "quota": {"available": 1234, "used": 766, "total": 2000},
                "availableUntil": "2026-10-01T00:00:00Z",
                "planCap": "HARD",
            }
        }
    )
    assert balance.remaining == 1234
    assert balance.used == 766
    assert balance.total == 2000
    assert balance.plan_cap == "HARD"
    assert balance.available_until == "2026-10-01T00:00:00Z"


def test_parse_credits_payload_fills_total_when_missing() -> None:
    balance = parse_credits_payload(
        {"total": {"quota": {"available": 10.9, "used": "3"}, "planCap": "free"}}
    )
    assert balance.remaining == 10
    assert balance.used == 3
    assert balance.total == 13
    assert balance.plan_cap == "FREE"


def test_parse_credits_payload_rejects_empty() -> None:
    try:
        parse_credits_payload({"foo": 1})
    except AdobeCreditsError as exc:
        assert exc.code == "invalid_response"
    else:
        raise AssertionError("expected AdobeCreditsError")


def test_require_proxy_url_rejects_blank() -> None:
    try:
        require_proxy_url("  ")
    except AdobeProxyRequired:
        return
    raise AssertionError("expected AdobeProxyRequired")


def test_fetch_credits_refuses_empty_proxy() -> None:
    try:
        fetch_credits_balance(
            access_token="token",
            account_id="acct",
            proxy_url="",
            user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            impersonate="chrome146",
        )
    except AdobeCreditsError as exc:
        assert exc.code == "proxy_required"
    else:
        raise AssertionError("expected AdobeCreditsError")


def test_token_exchange_refuses_empty_proxy() -> None:
    try:
        exchange_cookie_for_clio(
            cookie="ims_sid=x; other=y",
            proxy_url="",
            user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            impersonate="chrome146",
        )
    except AdobeAuthError as exc:
        assert "proxy" in str(exc).lower()
    else:
        raise AssertionError("expected AdobeAuthError")

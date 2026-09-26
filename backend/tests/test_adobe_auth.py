import base64
import json
from urllib.parse import parse_qs

import pytest

from app.adobe.auth import CLIO_SCOPE, TOKEN_URL, exchange_cookie_for_clio


def _jwt() -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"client_id": "clio-playground-web", "user_id": "account-id"}
        ).encode()
    ).decode().rstrip("=")
    return f"e30.{payload}.sig"


@pytest.mark.parametrize(
    ("cookie", "has_guest_allowed"),
    [
        ("ims_sid=bare-sid", True),
        ("ims_sid=full-sid; aux_sid=aux", False),
        ("ims_sid=full-sid; other=abc123", False),
    ],
)
def test_clio_mints_once_and_guest_allowed_only_for_bare_sid(
    monkeypatch: pytest.MonkeyPatch, cookie: str, has_guest_allowed: bool
) -> None:
    forms: list[dict[str, list[str]]] = []
    captured_headers: list[dict[str, str]] = []

    class FakeCookies:
        def get_dict(self) -> dict[str, str]:
            return {"ims_sid": "rotated-sid"}

    class FakeResponse:
        status_code = 200
        text = json.dumps({"access_token": _jwt(), "expires_in": 3600})
        content = text.encode()

        def json(self) -> dict[str, object]:
            return {"access_token": _jwt(), "expires_in": 3600}

    class FakeSession:
        cookies = FakeCookies()

        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, _url: str, **kwargs: object) -> FakeResponse:
            forms.append(parse_qs(str(kwargs["data"])))
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            captured_headers.append({str(key): str(value) for key, value in headers.items()})
            return FakeResponse()

    monkeypatch.setattr("app.adobe.auth.requests.Session", FakeSession)
    credentials = exchange_cookie_for_clio(
        cookie=cookie,
        proxy_url="http://user:pass@127.0.0.1:9",
        user_agent="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
        impersonate="chrome146",
    )

    assert len(forms) == 1
    assert ("guest_allowed" in forms[0]) is has_guest_allowed
    assert "tk_platform" not in CLIO_SCOPE
    assert "creative_production" in forms[0]["scope"][0]
    assert "profile" in forms[0]["scope"][0]
    assert credentials.token_kind == "clio"
    assert credentials.rotated_cookies == {"ims_sid": "rotated-sid"}
    mint = captured_headers[0]
    assert mint["sec-ch-ua-platform"] == '"Windows"'
    assert '"Google Chrome";v="148"' in mint["sec-ch-ua"]
    assert mint["Cookie"] == cookie
    assert TOKEN_URL == "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
    assert "jslVersion" not in TOKEN_URL


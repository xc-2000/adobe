import base64
import json

import pytest

from arp_harvester.browser import MintedArp, MintError, _build_headers, _validate_token


def make_token() -> str:
    return base64.b64encode(
        json.dumps({"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": "forter"}).encode()
    ).decode()


def test_validate_token_requires_complete_bundle() -> None:
    _validate_token(make_token())

    incomplete = base64.b64encode(json.dumps({"sid": "sid"}).encode()).decode()
    with pytest.raises(MintError, match="missing"):
        _validate_token(incomplete)


def test_build_headers_uses_browser_client_hints() -> None:
    headers = _build_headers(
        "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
        {
            "platform": "Windows",
            "platformVersion": "15.0.0",
            "architecture": "x86",
            "bitness": "64",
            "mobile": False,
            "model": "",
            "uaFullVersion": "148.0.7778.215",
            "fullVersionList": [
                {"brand": "Chromium", "version": "148.0.7778.215"},
                {"brand": "Google Chrome", "version": "148.0.7778.215"},
            ],
        },
    )

    assert headers["sec-ch-ua-platform"] == '"Windows"'
    assert '"Google Chrome";v="148"' in headers["sec-ch-ua"]
    assert headers["sec-ch-ua-full-version"] == '"148.0.7778.215"'


def test_minted_arp_repr_hides_token_and_proxy() -> None:
    minted = MintedArp(
        token=make_token(),
        proxy_url="http://user:password@proxy.example:3010",
        egress_id="sid-test",
        ua="Chrome/148.0.0.0",
        ch={},
        headers={},
    )

    rendered = repr(minted)
    assert "password" not in rendered
    assert make_token() not in rendered

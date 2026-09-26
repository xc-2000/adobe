import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from arp_harvester.browser import (
    MintedArp,
    MintError,
    _build_headers,
    _launch_args,
    _validate_token,
    close_browser,
    overlay_forter_cookie,
    require_matching_egress,
    require_public_ip,
    wait_sherlock,
)


def make_token() -> str:
    return base64.b64encode(
        json.dumps(
            {"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": "id_ts__UDF43-xx=-3-v2_tt"}
        ).encode()
    ).decode()


def test_validate_token_requires_complete_bundle() -> None:
    _validate_token(make_token())

    incomplete = base64.b64encode(json.dumps({"sid": "sid"}).encode()).decode()
    with pytest.raises(MintError, match="missing"):
        _validate_token(incomplete)


def test_validate_token_rejects_ftr_without_v2_tt() -> None:
    short = base64.b64encode(
        json.dumps({"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": "id_ts__UDF43-xx"}).encode()
    ).decode()
    with pytest.raises(MintError, match="v2_tt"):
        _validate_token(short)


def test_overlay_forter_cookie_prefers_full_v2_tt() -> None:
    short = "abc123_1720000000__UDF43-xx"
    full = short + "=-3-v2_tt"
    token = base64.b64encode(
        json.dumps({"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": short}).encode()
    ).decode()
    patched = overlay_forter_cookie(token, full)
    payload = json.loads(base64.b64decode(patched))
    assert payload["ftr"] == full
    assert overlay_forter_cookie(token, "") == token
    assert overlay_forter_cookie(token, "tiny") == token


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


def test_publish_payload_keeps_lease_url_verbatim() -> None:
    url = "http://p9mx-region-SG-sid-AbCdEfGh-t-10:pw-secret@us.arxlabs.io:3010"
    minted = MintedArp(
        token=make_token(),
        proxy_url=url,
        egress_id="sid-AbCdEfGh",
        ua="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
        ch={"platform": "Windows"},
        headers={"User-Agent": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36"},
    )
    payload = minted.publish_payload()
    assert payload["proxy_url"] == url
    assert payload["egress_id"] == "sid-AbCdEfGh"


@pytest.mark.asyncio
async def test_wait_sherlock_retries_until_ready() -> None:
    class Page:
        n = 0

        async def evaluate(self, _script: str) -> bool:
            self.n += 1
            return self.n >= 2

    await wait_sherlock(Page(), timeout_seconds=3)


@pytest.mark.asyncio
async def test_wait_sherlock_times_out() -> None:
    class Page:
        async def evaluate(self, _script: str) -> bool:
            return False

    with pytest.raises(MintError, match="not ready"):
        await wait_sherlock(Page(), timeout_seconds=1.2)


@pytest.mark.asyncio
async def test_close_browser_kills_when_close_hangs(monkeypatch: Any) -> None:
    killed: list[int] = []

    class Hang:
        async def close(self) -> None:
            await asyncio.sleep(30)

    monkeypatch.setattr("arp_harvester.browser.pid_alive", lambda _pid: True)
    monkeypatch.setattr("arp_harvester.browser.kill_tree", lambda pid: killed.append(pid) or True)
    started = time.monotonic()
    await close_browser(Hang(), 4242)
    assert time.monotonic() - started < 8
    assert killed == [4242]


def test_launch_args_are_fingerprint_chromium_148() -> None:
    args = _launch_args(4242)
    assert "--fingerprint=4242" in args
    assert "--fingerprint-platform=windows" in args
    assert "--fingerprint-brand=Chrome" in args
    assert "--fingerprint-webrtc-ip=auto" not in args


def test_resolve_chrome_binary_finds_exe_in_directory(tmp_path: Path) -> None:
    from arp_harvester.config import resolve_chrome_binary

    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"mz")
    assert resolve_chrome_binary(tmp_path) == chrome


def test_require_public_ip_rejects_private_and_empty() -> None:
    assert require_public_ip("8.8.8.8") == "8.8.8.8"
    with pytest.raises(MintError, match="proxy egress"):
        require_public_ip("127.0.0.1")
    with pytest.raises(MintError, match="proxy egress"):
        require_public_ip("10.0.0.1")
    with pytest.raises(MintError, match="proxy egress"):
        require_public_ip("")


def test_require_matching_egress_rejects_direct_exit() -> None:
    assert require_matching_egress("8.8.8.8", "8.8.8.8") == "8.8.8.8"
    with pytest.raises(MintError, match="lease proxy"):
        require_matching_egress("8.8.8.8", "1.1.1.1")


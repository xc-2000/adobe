from pathlib import Path

import pytest

from arp_harvester.proxies import ProxyFileError, load_proxy_file, parse_proxy, playwright_proxy


def test_parse_proxy_hides_credentials_and_preserves_sticky_sid() -> None:
    proxy = parse_proxy(
        "account-region-SG-sid-AbCd1234-t-10:password@proxy.example:3010",
        default_scheme="http",
    )

    assert proxy.egress_id == "sid-AbCd1234"
    assert proxy.url == (
        "http://account-region-SG-sid-AbCd1234-t-10:password@proxy.example:3010"
    )
    assert "password" not in repr(proxy)


def test_load_proxy_file_reports_line_without_secret(tmp_path: Path) -> None:
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("invalid-secret-value\n", encoding="utf-8")

    with pytest.raises(ProxyFileError) as error:
        load_proxy_file(proxy_file, default_scheme="http")

    assert "line 1" in str(error.value)
    assert "invalid-secret-value" not in str(error.value)


def test_parse_proxy_rejects_unknown_scheme() -> None:
    with pytest.raises(ProxyFileError, match="unsupported proxy scheme"):
        parse_proxy("ftp://user:password@proxy.example:21", default_scheme="http")


def test_playwright_proxy_strips_password_from_server() -> None:
    proxy = playwright_proxy(
        "http://account-region-SG-sid-AbCd1234-t-10:s3cret@proxy.example:3010"
    )
    assert proxy["server"] == "http://proxy.example:3010"
    assert proxy["username"] == "account-region-SG-sid-AbCd1234-t-10"
    assert proxy["password"] == "s3cret"
    assert "s3cret" not in proxy["server"]

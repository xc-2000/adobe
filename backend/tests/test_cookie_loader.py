from pathlib import Path

import pytest

from app.accounts.cookie_loader import CookieFileError, load_cookie_file


def test_load_cookie_file_returns_only_safe_metadata(tmp_path: Path) -> None:
    secret = "ims_sid=secret-value; aux_sid=another-secret; locale=en_US"
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(secret + "\n", encoding="utf-8")

    records = load_cookie_file(cookie_file)

    assert len(records) == 1
    assert records[0].value == secret
    assert records[0].has_ims_sid is True
    assert secret not in repr(records[0])
    assert "secret-value" not in str(records[0].safe_summary())
    assert records[0].safe_summary()["cookie_count"] == 3


def test_load_cookie_file_rejects_invalid_line(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("not-a-cookie\n", encoding="utf-8")

    with pytest.raises(CookieFileError, match="invalid Cookie header"):
        load_cookie_file(cookie_file)


def test_load_cookie_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CookieFileError, match="cookie file not found"):
        load_cookie_file(tmp_path / "missing.txt")

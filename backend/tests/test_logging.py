from app.core.logging import redact


def test_redact_removes_auth_cookie_and_proxy_credentials() -> None:
    message = (
        "Authorization: Bearer abc.def.ghi\n"
        "Cookie: ims_sid=secret; aux_sid=secret2\n"
        "proxy=https://user:password@example.com:443"
    )

    redacted = redact(message)

    assert isinstance(redacted, str)
    assert "abc.def.ghi" not in redacted
    assert "ims_sid" not in redacted
    assert "password" not in redacted
    assert redacted.count("[REDACTED]") == 3

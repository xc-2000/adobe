import pytest

from app.adobe.client import AdobeGenerateError, _nonce, _poll_url, _raise_submit_error


def test_nonce_is_stable_and_limits_prompt_prefix() -> None:
    prefix = "a" * 256
    assert _nonce("account", prefix + "one") == _nonce("account", prefix + "two")
    assert _nonce("account", "different") != _nonce("account", prefix)


def test_poll_url_normalizes_firefly_epo_host() -> None:
    raw = "https://firefly-epo1234.adobe.io/v2/jobs/result/job-1"

    normalized = _poll_url(raw)

    assert normalized == (
        "https://bks-epo1234.adobe.io/v2/jobs/result/job-1"
        "?host=firefly-epo1234.adobe.io/"
    )


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (408, '{"error_code":"timeout_error"}', "arp_rejected"),
        (401, "unauthorized", "auth_failed"),
        (403, "forbidden", "identity_rejected"),
        (429, "limited", "rate_limited"),
        (451, "image_unsafe", "content_rejected"),
        (500, "error", "upstream_error"),
    ],
)
def test_submit_error_classification(status: int, body: str, code: str) -> None:
    with pytest.raises(AdobeGenerateError) as error:
        _raise_submit_error(status, body)

    assert error.value.code == code

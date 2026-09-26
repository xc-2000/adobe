import inspect

import pytest
from pydantic import SecretStr

from app.adobe.auth import AdobeCredentials
from app.adobe.client import (
    AdobeGenerateError,
    GeneratedImage,
    _CLIO_POLL_KEYS,
    _CLIO_SUBMIT_KEYS,
    _generate_media,
    _nonce,
    _poll_headers,
    _poll_url,
    _proxied_call,
    _raise_submit_error,
    _submit_headers,
    adobe_result_urls,
    blob_id_from_upload,
    generate_image,
    generate_video,
    upload_reference_image,
)
from app.adobe.http import AdobeProxyRequired, is_transient_connect, optional_proxy_url
from app.adobe.trace import UpstreamTrace
from app.arp.models import ArpBundle


def test_nonce_is_stable_and_limits_prompt_prefix() -> None:
    prefix = "a" * 256
    assert _nonce("account", prefix + "one") == _nonce("account", prefix + "two")
    assert _nonce("account", "different") != _nonce("account", prefix)


def _bundle(**overrides: object) -> ArpBundle:
    data = {
        "id": "arp_x",
        "token": SecretStr("sid.ark.bfp.ftr"),
        "proxy_url": SecretStr("socks5h://user:pass@127.0.0.1:9"),
        "egress_id": "sid-x",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "ch": {
            "platform": "Windows",
            "mobile": False,
            "fullVersionList": [
                {"brand": "Chromium", "version": "148.0.7778.215"},
                {"brand": "Not?A_Brand", "version": "24.0.0.0"},
                {"brand": "Google Chrome", "version": "148.0.7778.215"},
            ],
        },
        "headers": {
            "User-Agent": "should-not-be-copied",
            "sec-ch-ua-full-version-list": "should-not-be-copied",
        },
        "minted_at": 1,
        "expires_at": 2,
        "max_uses": 1,
    }
    data.update(overrides)
    return ArpBundle.model_validate(data)


def test_ff_ios_headers_never_mix_web_arp_identity() -> None:
    credentials = AdobeCredentials(
        "ios-token", "account", 3600, token_kind="ff_ios"
    )
    headers = _submit_headers(None, credentials, "prompt")
    assert headers["x-api-key"] == "FF-iOS"
    assert headers["x-ims-clientid"] == "FF-iOS"
    assert "x-arp-session-id" not in headers
    assert "x-account-id" not in headers
    assert "x-request-id" not in headers
    assert not any(name.startswith("sec-ch-") for name in headers)
    poll = _poll_headers(None, credentials)
    assert "x-nonce" not in poll
    assert "content-type" not in poll
    assert "x-arp-session-id" not in poll


def test_clio_submit_headers_match_reference_set() -> None:
    credentials = AdobeCredentials("clio-token", "account", 3600, token_kind="clio")
    headers = _submit_headers(_bundle(), credentials, "a prompt")
    assert set(headers) == _CLIO_SUBMIT_KEYS
    assert headers["x-api-key"] == "clio-playground-web"
    assert headers["user-agent"].startswith("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    assert headers["sec-ch-ua-platform"] == '"Windows"'
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert '"Google Chrome";v="148"' in headers["sec-ch-ua"]
    assert headers["x-arp-session-id"] == "sid.ark.bfp.ftr"
    extra = {
        "x-account-id",
        "x-request-id",
        "accept-language",
        "User-Agent",
        "sec-ch-ua-full-version-list",
    }
    assert extra.isdisjoint(headers)
    poll = _poll_headers(_bundle(), credentials)
    assert set(poll) == _CLIO_POLL_KEYS
    assert "x-arp-session-id" not in poll
    assert "x-nonce" not in poll
    assert "content-type" not in poll
    assert poll["user-agent"] == headers["user-agent"]


def test_poll_url_normalizes_firefly_epo_host() -> None:
    raw = "https://firefly-epo1234.adobe.io/v2/jobs/result/job-1"

    normalized = _poll_url(raw)

    assert normalized == (
        "https://bks-epo1234.adobe.io/v2/jobs/result/job-1?host=firefly-epo1234.adobe.io/"
    )


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (408, '{"error_code":"timeout_error"}', "arp_rejected"),
        (401, "unauthorized", "auth_failed"),
        (403, "forbidden", "identity_rejected"),
        (429, "limited", "rate_limited"),
        (451, "image_unsafe", "content_rejected"),
        (
            400,
            '{"error_code":"bad_request","message":"1080p resolution is not supported '
            'for \'seedance_2.0_fast\'."}',
            "bad_request",
        ),
        (422, '{"message":"Field required"}', "bad_request"),
        (500, "error", "upstream_error"),
    ],
)
def test_submit_error_classification(status: int, body: str, code: str) -> None:
    with pytest.raises(AdobeGenerateError) as error:
        _raise_submit_error(status, body)

    assert error.value.code == code


def test_generate_image_refuses_empty_proxy() -> None:
    bundle = ArpBundle(
        id="arp_x",
        token=SecretStr("t"),
        proxy_url=SecretStr("   "),
        egress_id="sid-x",
        ua="Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
        ch={},
        headers={},
        minted_at=1,
        expires_at=2,
        max_uses=1,
    )
    creds = AdobeCredentials(access_token="tok", account_id="acct", expires_in=60)
    with pytest.raises(AdobeGenerateError) as error:
        generate_image(
            bundle=bundle,
            credentials=creds,
            payload={"prompt": "x"},
            impersonate="chrome146",
            poll_timeout_seconds=1,
        )
    assert error.value.code == "proxy_required"


def test_image_download_is_direct_not_proxied() -> None:
    src = inspect.getsource(_generate_media)
    assert "requests.get(media_url" in src
    assert "session.get(media_url" not in src
    assert "VIDEO_URL" in inspect.getsource(generate_video)


def test_blob_id_from_upload_reads_nested_images() -> None:
    assert blob_id_from_upload({"id": "top", "images": [{"id": "blob-1"}]}) == "blob-1"
    assert blob_id_from_upload({"imageId": "img-2"}) == "img-2"
    assert blob_id_from_upload({"id": "top"}) == "top"
    assert blob_id_from_upload({}) is None


def test_upload_via_proxy_flag_controls_optional_url() -> None:
    assert optional_proxy_url(False, "socks5h://u:p@h:1") is None
    assert optional_proxy_url(True, "socks5h://u:p@h:1") == "socks5h://u:p@h:1"
    try:
        optional_proxy_url(True, "  ")
    except AdobeProxyRequired:
        return
    raise AssertionError("expected AdobeProxyRequired")


def test_upload_reference_image_only_sets_proxies_when_asked() -> None:
    src = inspect.getsource(upload_reference_image)
    assert 'session_kwargs["proxies"]' in src
    assert "if proxy_url:" in src


def test_adobe_result_urls_collects_outputs_and_source() -> None:
    image = GeneratedImage(
        content=b"x",
        content_type="image/png",
        source_url="https://adobe.invalid/first.png",
        metadata={
            "outputs": [
                {"image": {"presignedUrl": "https://adobe.invalid/a.png"}},
                {"image": {"presignedUrl": "https://adobe.invalid/b.png"}},
            ]
        },
    )
    assert adobe_result_urls(image) == [
        "https://adobe.invalid/a.png",
        "https://adobe.invalid/b.png",
        "https://adobe.invalid/first.png",
    ]
    video = GeneratedImage(
        content=b"x",
        content_type="video/mp4",
        source_url="https://adobe.invalid/first.mp4",
        metadata={"outputs": [{"video": {"presignedUrl": "https://adobe.invalid/a.mp4"}}]},
    )
    assert adobe_result_urls(video) == [
        "https://adobe.invalid/a.mp4",
        "https://adobe.invalid/first.mp4",
    ]


def test_transient_connect_matches_tls_not_validation() -> None:
    assert is_transient_connect(OSError("SSL: WRONG_VERSION_NUMBER"))
    assert is_transient_connect(RuntimeError("curl: (35) OpenSSL SSL_connect"))
    assert not is_transient_connect(RuntimeError("validation_error Field required usage"))


def test_proxied_call_retries_then_raises_proxy_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adobe.client.time.sleep", lambda _s: None)

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_a: object, **_k: object) -> object:
            self.calls += 1
            raise OSError("SSL: WRONG_VERSION_NUMBER")

        def get(self, *_a: object, **_k: object) -> object:
            raise AssertionError("GET should not run")

    session = FakeSession()
    rec = UpstreamTrace()
    with pytest.raises(AdobeGenerateError) as error:
        _proxied_call(session, "POST", "https://example.invalid", rec=rec, step="submit")
    assert error.value.code == "proxy_tls"
    assert session.calls == 3


def test_proxied_call_recovers_after_two_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adobe.client.time.sleep", lambda _s: None)

    class Ok:
        status_code = 200
        content = b"{}"
        text = "{}"

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *_a: object, **_k: object) -> Ok:
            self.calls += 1
            if self.calls < 3:
                raise OSError("connection reset by peer")
            return Ok()

        def post(self, *_a: object, **_k: object) -> object:
            raise AssertionError("POST should not run")

    session = FakeSession()
    rec = UpstreamTrace()
    out = _proxied_call(session, "GET", "https://example.invalid", rec=rec, step="poll")
    assert session.calls == 3
    assert out.status_code == 200

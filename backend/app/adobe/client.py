from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests

from app.adobe.auth import AdobeCredentials
from app.adobe.ff_ios import poll_headers as ios_poll_headers
from app.adobe.ff_ios import submit_headers as ios_submit_headers
from app.adobe.http import AdobeProxyRequired, is_transient_connect, session_proxies
from app.adobe.payloads import build_image_payload
from app.adobe.trace import UpstreamTrace, parse_response_body
from app.arp.models import ArpBundle

GENERATE_URL = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
VIDEO_URL = "https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async"
_CONNECT_TRIES = 3
_POLL_SLEEP_SECONDS = 1.0


class AdobeGenerateError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    content: bytes
    content_type: str
    source_url: str
    metadata: dict[str, Any]


def _nonce(account_id: str, prompt: str) -> str:
    normalized = prompt.strip()[:256]
    return hashlib.sha256(f"{account_id}-{normalized}".encode()).hexdigest()


def _poll_url(raw: str) -> str:
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if not host.startswith("firefly-epo"):
        return raw
    suffix = host.removeprefix("firefly-epo").split(".", 1)[0]
    job_id = parsed.path.rstrip("/").split("/")[-1]
    if len(suffix) == 4 and suffix.isdigit() and job_id:
        return f"https://bks-epo{suffix}.adobe.io/v2/jobs/result/{job_id}?host={parsed.netloc}/"
    return raw


_FIREFLY_WEB = "https://firefly.adobe.com"
_CLIO_SUBMIT_KEYS = frozenset(
    {
        "accept",
        "origin",
        "referer",
        "user-agent",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "x-api-key",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "authorization",
        "content-type",
        "x-nonce",
        "x-arp-session-id",
    }
)
_CLIO_POLL_KEYS = _CLIO_SUBMIT_KEYS - {"content-type", "x-nonce", "x-arp-session-id"}


def _arp_identity_headers(bundle: ArpBundle) -> dict[str, str]:
    """Overlay the minting browser only — same four fields as the reference client."""
    headers = {"user-agent": bundle.ua}
    ch = bundle.ch if isinstance(bundle.ch, dict) else {}
    compact: list[str] = []
    versions = ch.get("fullVersionList")
    if isinstance(versions, list):
        for item in versions:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").strip()
            version = str(item.get("version") or "").strip()
            if brand and version:
                compact.append(f'"{brand}";v="{version.split(".", 1)[0]}"')
    if compact:
        headers["sec-ch-ua"] = ", ".join(compact)
    else:
        harvested = (bundle.headers or {}).get("sec-ch-ua")
        headers["sec-ch-ua"] = harvested or (
            '"Chromium";v="148", "Not?A_Brand";v="24", "Google Chrome";v="148"'
        )
    platform = str(ch.get("platform") or "").strip()
    if platform:
        headers["sec-ch-ua-platform"] = f'"{platform}"'
    else:
        harvested_platform = (bundle.headers or {}).get("sec-ch-ua-platform")
        headers["sec-ch-ua-platform"] = harvested_platform or '"Windows"'
    if "mobile" in ch:
        headers["sec-ch-ua-mobile"] = "?1" if ch.get("mobile") else "?0"
    else:
        headers["sec-ch-ua-mobile"] = "?0"
    return headers


def _clio_common_headers(bundle: ArpBundle, credentials: AdobeCredentials) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "origin": _FIREFLY_WEB,
        "referer": f"{_FIREFLY_WEB}/",
        "x-api-key": "clio-playground-web",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "authorization": f"Bearer {credentials.access_token}",
    }
    headers.update(_arp_identity_headers(bundle))
    return headers


def _submit_headers(
    bundle: ArpBundle | None,
    credentials: AdobeCredentials,
    prompt: str,
) -> dict[str, str]:
    if credentials.token_kind == "ff_ios":
        return {
            **ios_submit_headers(credentials.access_token, credentials.account_id, prompt),
            "content-type": "application/json",
        }
    if bundle is None:
        raise AdobeGenerateError("arp_empty", 503, "alpha submission requires ARP")
    headers = _clio_common_headers(bundle, credentials)
    headers["content-type"] = "application/json"
    headers["x-arp-session-id"] = bundle.token.get_secret_value()
    headers["x-nonce"] = _nonce(credentials.account_id, prompt)
    extra = set(headers) - _CLIO_SUBMIT_KEYS
    missing = _CLIO_SUBMIT_KEYS - set(headers)
    if extra or missing:
        raise AdobeGenerateError(
            "invalid_headers",
            500,
            "clio submit headers drifted from the reference set",
        )
    return headers


def _poll_headers(
    bundle: ArpBundle | None,
    credentials: AdobeCredentials,
) -> dict[str, str]:
    if credentials.token_kind == "ff_ios":
        return ios_poll_headers(credentials.access_token)
    if bundle is None:
        raise AdobeGenerateError("arp_empty", 503, "alpha submission requires ARP")
    headers = _clio_common_headers(bundle, credentials)
    extra = set(headers) - _CLIO_POLL_KEYS
    missing = _CLIO_POLL_KEYS - set(headers)
    if extra or missing:
        raise AdobeGenerateError(
            "invalid_headers",
            500,
            "clio poll headers drifted from the reference set",
        )
    return headers


def _adobe_error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error_message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
            if isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:300]
    text = body.strip()
    return text[:300] if text else "generation request failed"


def _raise_submit_error(status_code: int, body: str) -> None:
    lowered = body.lower()
    if status_code == 408 or "system under load" in lowered or "timeout_error" in lowered:
        raise AdobeGenerateError("arp_rejected", status_code, "408 异常")
    if status_code == 401:
        raise AdobeGenerateError("auth_failed", status_code, "账号鉴权失败")
    if status_code == 403:
        raise AdobeGenerateError("identity_rejected", status_code, "账号被拒绝")
    if status_code == 429:
        raise AdobeGenerateError("rate_limited", status_code, "429 频繁")
    if status_code == 451:
        raise AdobeGenerateError("content_rejected", status_code, "451 违禁词")
    if status_code in {400, 422}:
        raise AdobeGenerateError("bad_request", status_code, _adobe_error_message(body))
    raise AdobeGenerateError("upstream_error", status_code, "生成失败")


def _extract_poll_url(response: requests.Response, payload: dict[str, Any]) -> str:
    override = response.headers.get("x-override-status-link", "").strip()
    if override:
        return _poll_url(override)
    links = payload.get("links")
    if isinstance(links, dict):
        result = links.get("result")
        if isinstance(result, dict) and result.get("href"):
            return _poll_url(str(result["href"]))
        if isinstance(result, str) and result:
            return _poll_url(result)
    raise AdobeGenerateError("invalid_response", 200, "Adobe returned no poll URL")


def _find_media_url(payload: dict[str, Any], kind: str) -> str | None:
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return None
    first = outputs[0]
    if not isinstance(first, dict):
        return None
    media = first.get(kind)
    if not isinstance(media, dict):
        return None
    value = media.get("presignedUrl") or media.get("url")
    return str(value) if value else None


def _find_image_url(payload: dict[str, Any]) -> str | None:
    return _find_media_url(payload, "image")


def raise_if_connect_drop(exc: BaseException) -> None:
    if is_transient_connect(exc):
        raise AdobeGenerateError("proxy_tls", 502, "upstream connection dropped") from exc


def _proxied_call(
    session: requests.Session[requests.Response],
    method: str,
    url: str,
    *,
    rec: UpstreamTrace,
    step: str,
    **kwargs: Any,
) -> requests.Response:
    last: BaseException | None = None
    for attempt in range(_CONNECT_TRIES):
        started = time.perf_counter()
        try:
            if method == "POST":
                return session.post(url, **kwargs)
            return session.get(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            rec.record(
                step,
                method=method,
                url=url,
                error=str(exc)[:400],
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            if is_transient_connect(exc) and attempt + 1 < _CONNECT_TRIES:
                time.sleep(1.0)
                continue
            raise_if_connect_drop(exc)
            raise
    if last is not None:
        raise_if_connect_drop(last)
        raise last
    raise AdobeGenerateError("upstream_error", 500, "Adobe request failed")


def adobe_result_urls(image: GeneratedImage) -> list[str]:
    """Presigned/result URLs Adobe returned. Never log these."""
    found: list[str] = []
    meta = image.metadata if isinstance(image.metadata, dict) else {}
    outputs = meta.get("outputs")
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            for kind in ("image", "video"):
                media = item.get(kind)
                if not isinstance(media, dict):
                    continue
                value = media.get("presignedUrl") or media.get("url")
                if value:
                    url = str(value)
                    if url not in found:
                        found.append(url)
    if image.source_url and image.source_url not in found:
        found.append(image.source_url)
    return found


def _generate_media(
    *,
    bundle: ArpBundle | None,
    credentials: AdobeCredentials,
    payload: dict[str, Any],
    impersonate: str,
    poll_timeout_seconds: int,
    submit_url: str,
    media_kind: str,
    default_content_type: str,
    proxy_url: str | None = None,
    trace: UpstreamTrace | None = None,
) -> GeneratedImage:
    prompt = str(payload.get("prompt") or "")
    rec = trace or UpstreamTrace()
    try:
        selected_proxy = (
            bundle.proxy_url.get_secret_value() if bundle is not None else proxy_url or ""
        )
        proxies = session_proxies(selected_proxy)
    except AdobeProxyRequired as exc:
        rec.record(
            "submit", method="POST", url=submit_url, status_code=422, error="proxy_required"
        )
        raise AdobeGenerateError("proxy_required", 422, "Adobe requests require a proxy") from exc
    session: requests.Session[requests.Response] = requests.Session(
        impersonate=impersonate,
        proxies=proxies,
    )
    started = time.perf_counter()
    response = _proxied_call(
        session,
        "POST",
        submit_url,
        rec=rec,
        step="submit",
        headers=_submit_headers(bundle, credentials, prompt),
        json=payload,
        timeout=60,
    )
    submit_body = parse_response_body(response.content, response.text)
    rec.record(
        "submit",
        method="POST",
        url=submit_url,
        status_code=response.status_code,
        body=submit_body,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error=None if response.status_code == 200 else f"HTTP {response.status_code}",
    )
    if response.status_code != 200:
        _raise_submit_error(response.status_code, response.text[:500])
    submit_payload = submit_body if isinstance(submit_body, dict) else json.loads(response.content)
    if not isinstance(submit_payload, dict):
        raise AdobeGenerateError("invalid_response", 200, "Adobe submit returned invalid JSON")
    poll_url = _extract_poll_url(response, submit_payload)

    deadline = time.monotonic() + poll_timeout_seconds
    poll_headers = _poll_headers(bundle, credentials)
    while time.monotonic() < deadline:
        started = time.perf_counter()
        poll = _proxied_call(
            session, "GET", poll_url, rec=rec, step="poll", headers=poll_headers, timeout=60
        )
        poll_body = parse_response_body(poll.content, poll.text)
        rec.record(
            "poll",
            method="GET",
            url=poll_url,
            status_code=poll.status_code,
            body=poll_body,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=None if poll.status_code == 200 else f"HTTP {poll.status_code}",
        )
        if poll.status_code != 200:
            _raise_submit_error(poll.status_code, poll.text[:500])
        poll_payload = poll_body if isinstance(poll_body, dict) else json.loads(poll.content)
        if not isinstance(poll_payload, dict):
            raise AdobeGenerateError("invalid_response", 200, "Adobe poll returned invalid JSON")
        media_url = _find_media_url(poll_payload, media_kind)
        if media_url:
            started = time.perf_counter()
            downloaded = requests.get(media_url, impersonate=impersonate, timeout=120)
            rec.record(
                "download",
                method="GET",
                url=media_url,
                status_code=downloaded.status_code,
                body={
                    "content_type": downloaded.headers.get("content-type", ""),
                    "bytes": len(downloaded.content or b""),
                },
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=None if downloaded.status_code == 200 else f"HTTP {downloaded.status_code}",
            )
            if downloaded.status_code != 200:
                raise AdobeGenerateError(
                    "download_failed",
                    downloaded.status_code,
                    "结果下载失败",
                )
            header_type = downloaded.headers.get("content-type", "") or default_content_type
            body = downloaded.content or b""
            looks_video = "video" in header_type.lower() or "mp4" in header_type.lower()
            if media_kind == "video" and not looks_video:
                header_type = default_content_type
            if len(body) >= 8 and body[4:8] == b"ftyp":
                header_type = "video/mp4"
            return GeneratedImage(
                content=body,
                content_type=header_type,
                source_url=media_url,
                metadata=poll_payload,
            )
        status = str(poll_payload.get("status") or "").upper()
        if status in {"FAILED", "CANCELLED", "ERROR"}:
            raise AdobeGenerateError("job_failed", 200, "生成失败")
        time.sleep(_POLL_SLEEP_SECONDS)
    rec.record("poll", method="GET", url=poll_url, status_code=504, error="poll_timeout")
    raise AdobeGenerateError("poll_timeout", 504, "生成超时")


def generate_image(
    *,
    bundle: ArpBundle | None,
    credentials: AdobeCredentials,
    payload: dict[str, Any],
    impersonate: str,
    poll_timeout_seconds: int,
    proxy_url: str | None = None,
    trace: UpstreamTrace | None = None,
) -> GeneratedImage:
    return _generate_media(
        bundle=bundle,
        credentials=credentials,
        payload=payload,
        impersonate=impersonate,
        poll_timeout_seconds=poll_timeout_seconds,
        submit_url=GENERATE_URL,
        media_kind="image",
        default_content_type="image/png",
        proxy_url=proxy_url,
        trace=trace,
    )


def generate_video(
    *,
    bundle: ArpBundle | None,
    credentials: AdobeCredentials,
    payload: dict[str, Any],
    impersonate: str,
    poll_timeout_seconds: int,
    proxy_url: str | None = None,
    trace: UpstreamTrace | None = None,
) -> GeneratedImage:
    return _generate_media(
        bundle=bundle,
        credentials=credentials,
        payload=payload,
        impersonate=impersonate,
        poll_timeout_seconds=poll_timeout_seconds,
        submit_url=VIDEO_URL,
        media_kind="video",
        default_content_type="video/mp4",
        proxy_url=proxy_url,
        trace=trace,
    )


def generate_smoke_image(
    *,
    bundle: ArpBundle,
    credentials: AdobeCredentials,
    prompt: str,
    impersonate: str,
    poll_timeout_seconds: int,
) -> GeneratedImage:
    payload = build_image_payload(
        model="gpt-image-2", prompt=prompt, resolution="1K", aspect_ratio="1:1"
    )
    payload["caiClaimVersion"] = 2
    return generate_image(
        bundle=bundle,
        credentials=credentials,
        payload=payload,
        impersonate=impersonate,
        poll_timeout_seconds=poll_timeout_seconds,
    )


UPLOAD_IMAGE_URL = "https://firefly-3p.ff.adobe.io/v2/storage/image"


def blob_id_from_upload(payload: dict[str, Any]) -> str | None:
    for key in ("images", "videos", "audios", "audio", "assets", "files"):
        items = payload.get(key)
        if not isinstance(items, list) or not items:
            continue
        first = items[0]
        if isinstance(first, dict):
            ident = first.get("id")
            if isinstance(ident, str) and ident.strip():
                return ident.strip()
    for key in ("imageId", "id"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def upload_reference_image(
    *,
    content: bytes,
    content_type: str,
    credentials: AdobeCredentials,
    user_agent: str,
    impersonate: str,
    proxy_url: str | None = None,
    trace: UpstreamTrace | None = None,
) -> str:
    """Upload a reference image to Adobe storage.

    ``proxy_url`` None = 直连（默认）。非空则走 sticky 代理，由配置
    ``proxy.upload_via_proxy`` 决定调用方是否传入。
    """
    session_kwargs: dict[str, Any] = {"impersonate": impersonate}
    if proxy_url:
        session_kwargs["proxies"] = session_proxies(proxy_url)
    rec = trace or UpstreamTrace()
    started = time.perf_counter()
    with requests.Session(**session_kwargs) as session:
        response = _proxied_call(
            session,
            "POST",
            UPLOAD_IMAGE_URL,
            rec=rec,
            step="upload",
            headers={
                "authorization": f"Bearer {credentials.access_token}",
                "x-api-key": (
                    "FF-iOS" if credentials.token_kind == "ff_ios" else "clio-playground-web"
                ),
                "content-type": content_type or "image/png",
                "accept": "*/*",
                "user-agent": user_agent,
            },
            data=content,
            timeout=120,
        )
    body = parse_response_body(response.content, response.text)
    if trace is not None:
        trace.record(
            "upload",
            method="POST",
            url=UPLOAD_IMAGE_URL,
            status_code=response.status_code,
            body=body,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=None if response.status_code == 200 else f"HTTP {response.status_code}",
        )
    if response.status_code != 200:
        _raise_submit_error(response.status_code, response.text[:500])
    payload = body if isinstance(body, dict) else None
    if payload is None:
        try:
            payload = json.loads(response.content)
        except Exception as exc:
            raise AdobeGenerateError(
                "invalid_response", 200, "Adobe upload returned invalid JSON"
            ) from exc
    if not isinstance(payload, dict):
        raise AdobeGenerateError("invalid_response", 200, "Adobe upload returned invalid JSON")
    blob_id = blob_id_from_upload(payload)
    if not blob_id:
        raise AdobeGenerateError("invalid_response", 200, "Adobe upload returned no blob id")
    return blob_id

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from curl_cffi import requests

from app.adobe.auth import AdobeCredentials
from app.arp.models import ArpBundle

GENERATE_URL = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"


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
        return (
            f"https://bks-epo{suffix}.adobe.io/v2/jobs/result/{job_id}"
            f"?host={parsed.netloc}/"
        )
    return raw


def _submit_headers(
    bundle: ArpBundle,
    credentials: AdobeCredentials,
    prompt: str,
) -> dict[str, str]:
    headers = dict(bundle.headers)
    headers.update(
        {
            "authorization": f"Bearer {credentials.access_token}",
            "x-api-key": "clio-playground-web",
            "x-account-id": credentials.account_id,
            "x-arp-session-id": bundle.token.get_secret_value(),
            "x-nonce": _nonce(credentials.account_id, prompt),
            "x-request-id": str(uuid4()),
            "accept": "*/*",
            "content-type": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://firefly.adobe.com",
            "referer": "https://firefly.adobe.com/",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
    )
    return headers


def _raise_submit_error(status_code: int, body: str) -> None:
    lowered = body.lower()
    if status_code == 408 or "system under load" in lowered or "timeout_error" in lowered:
        raise AdobeGenerateError("arp_rejected", status_code, "Adobe rejected the ARP identity")
    if status_code == 401:
        raise AdobeGenerateError("auth_failed", status_code, "Adobe access token is invalid")
    if status_code == 403:
        raise AdobeGenerateError("identity_rejected", status_code, "Adobe rejected the identity")
    if status_code == 429:
        raise AdobeGenerateError("rate_limited", status_code, "Adobe account is rate limited")
    if status_code == 451:
        raise AdobeGenerateError("content_rejected", status_code, "Adobe rejected the content")
    raise AdobeGenerateError("upstream_error", status_code, "Adobe generation request failed")


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


def _find_image_url(payload: dict[str, Any]) -> str | None:
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return None
    first = outputs[0]
    if not isinstance(first, dict):
        return None
    image = first.get("image")
    if not isinstance(image, dict):
        return None
    value = image.get("presignedUrl")
    return str(value) if value else None


def generate_smoke_image(
    *,
    bundle: ArpBundle,
    credentials: AdobeCredentials,
    prompt: str,
    impersonate: str,
    poll_timeout_seconds: int,
) -> GeneratedImage:
    proxies: requests.ProxySpec = {
        "http": bundle.proxy_url.get_secret_value(),
        "https": bundle.proxy_url.get_secret_value(),
    }
    payload = {
        "modelId": "gpt-image",
        "modelVersion": "2",
        "n": 1,
        "prompt": prompt,
        "seeds": [int.from_bytes(uuid4().bytes[:4], "big") % 999999],
        "output": {"storeInputs": True},
        "referenceBlobs": [],
        "generationMetadata": {
            "module": "text2image",
            "submodule": "ff-image-generate",
        },
        "modelSpecificPayload": {"size": "1024x1024"},
        "generationSettings": {"detailLevel": 3},
        "caiClaimVersion": 2,
    }
    session: requests.Session[requests.Response] = requests.Session(
        impersonate=impersonate,
        proxies=proxies,
    )
    response = session.post(
        GENERATE_URL,
        headers=_submit_headers(bundle, credentials, prompt),
        json=payload,
        timeout=60,
    )
    if response.status_code != 200:
        _raise_submit_error(response.status_code, response.text[:500])
    submit_payload = json.loads(response.content)
    if not isinstance(submit_payload, dict):
        raise AdobeGenerateError("invalid_response", 200, "Adobe submit returned invalid JSON")
    poll_url = _extract_poll_url(response, submit_payload)

    deadline = time.monotonic() + poll_timeout_seconds
    poll_headers = {
        "authorization": f"Bearer {credentials.access_token}",
        "accept": "*/*",
        "origin": "https://firefly.adobe.com",
        "referer": "https://firefly.adobe.com/",
        "user-agent": bundle.ua,
    }
    while time.monotonic() < deadline:
        poll = session.get(poll_url, headers=poll_headers, timeout=60)
        if poll.status_code != 200:
            _raise_submit_error(poll.status_code, poll.text[:500])
        poll_payload = json.loads(poll.content)
        if not isinstance(poll_payload, dict):
            raise AdobeGenerateError("invalid_response", 200, "Adobe poll returned invalid JSON")
        image_url = _find_image_url(poll_payload)
        if image_url:
            direct = requests.get(image_url, impersonate=impersonate, timeout=120)
            if direct.status_code != 200:
                raise AdobeGenerateError(
                    "download_failed",
                    direct.status_code,
                    "Generated image download failed",
                )
            return GeneratedImage(
                content=direct.content,
                content_type=direct.headers.get("content-type", "image/png"),
                source_url=image_url,
                metadata=poll_payload,
            )
        status = str(poll_payload.get("status") or "").upper()
        if status in {"FAILED", "CANCELLED", "ERROR"}:
            raise AdobeGenerateError("job_failed", 200, "Adobe generation job failed")
        time.sleep(3)
    raise AdobeGenerateError("poll_timeout", 504, "Adobe generation timed out")

"""Resolve panel/API reference images into bytes before Adobe storage upload.

Local files, ``file:`` staging tokens, data URI and raw base64 are already
bytes — those paths do not HTTP-download. Only ``http(s)://`` is fetched,
because Adobe storage will not pull a URL. Adobe generate still only receives
the uploaded blob id.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from pathlib import Path

from app.adobe.client import AdobeGenerateError

MAX_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED = frozenset({"image/jpeg", "image/png", "image/webp"})


def canonical_image_mime(value: str) -> str:
    mime = (value or "").split(";", 1)[0].strip().lower()
    if mime in {"image/jpg", "image/pjpeg"}:
        return "image/jpeg"
    if mime == "image/x-png":
        return "image/png"
    return mime


def sniff_image_mime(data: bytes, declared: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    mime = canonical_image_mime(declared)
    if mime in _ALLOWED:
        return mime
    raise AdobeGenerateError(
        "invalid_response", 400, "reference image must be jpeg, png, or webp"
    )


def _decode_base64(raw: str) -> bytes:
    compact = "".join(raw.split())
    padding = "=" * ((4 - len(compact) % 4) % 4)
    try:
        return base64.b64decode(compact + padding, validate=False)
    except Exception as exc:
        raise AdobeGenerateError("invalid_response", 400, "invalid reference image base64") from exc


def _bounded(data: bytes) -> bytes:
    if not data:
        raise AdobeGenerateError("invalid_response", 400, "reference image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise AdobeGenerateError("invalid_response", 400, "reference image is too large")
    return data


def _from_data_uri(token: str) -> tuple[bytes, str]:
    header, _, payload = token.partition(",")
    if not payload:
        raise AdobeGenerateError("invalid_response", 400, "invalid data URI")
    declared = "image/jpeg"
    if ";" in header:
        declared = header.split(":", 1)[-1].split(";", 1)[0]
    data = _bounded(_decode_base64(payload))
    return data, sniff_image_mime(data, declared)


def _from_url(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"accept": "image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
            declared = str(response.headers.get("content-type") or "")
    except urllib.error.HTTPError as exc:
        raise AdobeGenerateError("invalid_response", 400, "reference URL fetch failed") from exc
    except Exception as exc:
        raise AdobeGenerateError("invalid_response", 400, "reference URL fetch failed") from exc
    data = _bounded(data)
    return data, sniff_image_mime(data, declared)


def _from_logo(media_dir: Path) -> tuple[bytes, str]:
    logo = Path(media_dir).parent / "logo.png"
    if not logo.is_file():
        raise AdobeGenerateError("invalid_response", 400, "local logo.png missing")
    data = _bounded(logo.read_bytes())
    return data, sniff_image_mime(data, "image/png")


def _from_staged_file(token: str, media_dir: Path) -> tuple[bytes, str]:
    rel = token[5:].replace("\\", "/").lstrip("/")
    root = Path(media_dir).resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AdobeGenerateError("invalid_response", 400, "reference file is missing") from exc
    if not path.is_file():
        raise AdobeGenerateError("invalid_response", 400, "reference file is missing")
    data = _bounded(path.read_bytes())
    return data, sniff_image_mime(data)


def stage_reference_bytes(
    media_dir: Path, job_id: str, index: int, data: bytes, declared: str = ""
) -> str:
    """Persist uploaded bytes under media_dir; return a ``file:`` token."""
    payload = _bounded(data)
    mime = sniff_image_mime(payload, declared)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
    folder = Path(media_dir) / "refs" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{index}{ext}"
    dest.write_bytes(payload)
    return f"file:refs/{job_id}/{dest.name}"


def load_reference_image(token: str, *, media_dir: Path) -> tuple[bytes, str]:
    """Return ``(bytes, content-type)`` for one reference input.

    Local files / data URI / base64 are already bytes — no HTTP download.
    Only ``http(s)://`` is fetched, because Adobe storage will not pull a URL.
    """
    value = (token or "").strip()
    if not value:
        raise AdobeGenerateError("invalid_response", 400, "reference image is empty")
    if value == "local:logo":
        return _from_logo(media_dir)
    if value.startswith("file:"):
        return _from_staged_file(value, media_dir)
    if value.startswith("data:"):
        return _from_data_uri(value)
    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return _from_url(value)
    data = _bounded(_decode_base64(value))
    return data, sniff_image_mime(data, "image/jpeg")

"""Sanitize and collect per-step Adobe responses for the admin request-log detail.

Never stores cookies, access tokens, or proxy credentials.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.logging import redact

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "cookie",
        "password",
        "proxy_url",
        "proxy",
        "token",
        "client_secret",
        "x-api-key",
        "ims_sid",
        "arkose",
        "bfp",
        "ftr",
    }
)
_MAX_STR = 8_000
_KEEP_POLL_HEAD = 4
_KEEP_POLL_TAIL = 8


def sanitize_url(url: str) -> str:
    cleaned = redact(url.strip())
    if not isinstance(cleaned, str):
        return ""
    parts = urlsplit(cleaned)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = host
        cleaned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return cleaned[:500]


def sanitize_body(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        return redact(value[:_MAX_STR])
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in _SECRET_KEYS:
                out[name] = "[REDACTED]"
            else:
                out[name] = sanitize_body(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        if len(value) > 20:
            head = [sanitize_body(item, depth=depth + 1) for item in value[:10]]
            return head + [f"… {len(value) - 10} more"]
        return [sanitize_body(item, depth=depth + 1) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value)[:_MAX_STR])


def parse_response_body(content: bytes, text: str) -> Any:
    if not content:
        return None
    try:
        import json

        return json.loads(content)
    except Exception:  # noqa: BLE001
        return text[:_MAX_STR]


class UpstreamTrace:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def record(
        self,
        step: str,
        *,
        method: str = "",
        url: str = "",
        status_code: int = 0,
        body: Any = None,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        row: dict[str, Any] = {
            "step": step,
            "method": method,
            "url": sanitize_url(url) if url else "",
            "status_code": int(status_code or 0),
            "latency_ms": max(0, int(latency_ms)),
        }
        if body is not None:
            row["body"] = sanitize_body(body)
        if error:
            row["error"] = redact(str(error)[:800])
        self.steps.append(row)

    def export(self) -> list[dict[str, Any]]:
        polls = [i for i, row in enumerate(self.steps) if row.get("step") == "poll"]
        keep_tail = _KEEP_POLL_HEAD + _KEEP_POLL_TAIL
        if len(polls) <= keep_tail:
            return list(self.steps)
        drop = set(polls[_KEEP_POLL_HEAD : len(polls) - _KEEP_POLL_TAIL])
        out: list[dict[str, Any]] = []
        skipped = 0
        for index, row in enumerate(self.steps):
            if index in drop:
                skipped += 1
                continue
            if skipped:
                out.append({"step": "poll", "note": f"省略 {skipped} 次中间轮询"})
                skipped = 0
            out.append(row)
        return out

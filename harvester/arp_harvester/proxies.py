from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

_SID_PATTERN = re.compile(r"(?:^|-)sid-([A-Za-z0-9]+)(?:-|$)")


class ProxyFileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProxyRecord:
    scheme: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def url(self) -> str:
        username = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"{self.scheme}://{username}:{password}@{self.host}:{self.port}"

    @property
    def egress_id(self) -> str:
        match = _SID_PATTERN.search(self.username)
        if match:
            return f"sid-{match.group(1)}"
        digest = hashlib.sha256(self.username.encode()).hexdigest()[:12]
        return f"proxy-{digest}"


def parse_proxy(value: str, *, default_scheme: str) -> ProxyRecord:
    raw = value.strip()
    if not raw:
        raise ProxyFileError("empty proxy")

    scheme = default_scheme
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ProxyFileError("unsupported proxy scheme")

    credentials, separator, address = raw.rpartition("@")
    if not separator:
        raise ProxyFileError("proxy credentials are required")
    username, separator, password = credentials.partition(":")
    if not separator or not username or not password:
        raise ProxyFileError("invalid proxy credentials")
    host, separator, port_text = address.rpartition(":")
    if not separator or not host:
        raise ProxyFileError("invalid proxy address")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ProxyFileError("invalid proxy port") from exc
    if not 1 <= port <= 65535:
        raise ProxyFileError("proxy port is out of range")
    return ProxyRecord(
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
    )


def load_proxy_file(path: Path, *, default_scheme: str) -> list[ProxyRecord]:
    if not path.is_file():
        raise ProxyFileError(f"proxy file not found: {path}")
    proxies: list[ProxyRecord] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                proxies.append(parse_proxy(line, default_scheme=default_scheme))
            except ProxyFileError as exc:
                raise ProxyFileError(f"line {line_number}: {exc}") from exc
    if not proxies:
        raise ProxyFileError("proxy file contains no proxies")
    return proxies


def playwright_proxy(value: str) -> dict[str, str]:
    """SOCKS only. HTTP sticky auth goes through AuthProxyRelay, not this dict."""
    record = parse_proxy(value, default_scheme="http")
    scheme = "socks5" if record.scheme in {"socks5", "socks5h"} else record.scheme
    return {
        "server": f"{scheme}://{record.host}:{record.port}",
        "username": unquote(record.username),
        "password": unquote(record.password),
    }

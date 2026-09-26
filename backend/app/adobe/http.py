"""Shared proxy guard for Adobe upstream HTTP that must share mint egress.

换 AT、查额度、提交、轮询必须带 sticky proxy URL。空字符串会让 curl_cffi 直连机房出口。
下图始终直连。传图是否走代理由配置中心 `proxy.upload_via_proxy` 决定。
"""

from __future__ import annotations

from curl_cffi import requests


class AdobeProxyRequired(RuntimeError):
    pass


_CONNECT_DROP = (
    "wrong_version_number",
    "ssl",
    "tls",
    "eof occurred",
    "connection reset",
    "connection closed",
    "curl: (35)",
    "curl: (56)",
    "curl: (7)",
    "curl: (28)",
    "timed out",
    "timeout",
)


def is_transient_connect(exc: BaseException) -> bool:
    """Proxy/TLS drop. Never treat Adobe 4xx validation as this."""
    text = str(exc).lower()
    return any(needle in text for needle in _CONNECT_DROP)


def require_proxy_url(proxy_url: str) -> str:
    value = (proxy_url or "").strip()
    if not value:
        raise AdobeProxyRequired("Adobe upstream calls require a proxy")
    return value


def optional_proxy_url(enabled: bool, proxy_url: str) -> str | None:
    """传图：关闭则直连；开启则必须有 sticky URL。"""
    if not enabled:
        return None
    return require_proxy_url(proxy_url)


def session_proxies(proxy_url: str) -> requests.ProxySpec:
    url = require_proxy_url(proxy_url)
    return {"http": url, "https": url}

"""Mint-loop policy. Keep this free of I/O so it can be unit-tested without CloakBrowser."""

from __future__ import annotations

_PROXY_CONNECT_MARKERS = (
    "err_empty_response",
    "err_tunnel_connection_failed",
    "err_proxy_connection_failed",
    "err_no_supported_proxies",
    "proxy connect",
    "connect aborted",
    "lease proxy connect",
    "browser did not use the lease proxy",
    "minted without proxy egress",
    "browser proxy egress check failed",
)


def is_proxy_connect_failure(exc: BaseException) -> bool:
    """True when Chrome/curl never established CONNECT. Do not hammer the pool."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _PROXY_CONNECT_MARKERS)


def pause_at(pool_target: int, min_ready: int = 0) -> int:
    """Stop minting once the standing pool is full. Browser count is not the target."""
    return max(int(pool_target), int(min_ready), 1)


def should_pause_mint(ready: int, pool_target: int, min_ready: int = 0) -> bool:
    return ready >= pause_at(pool_target, min_ready)


def should_skip_idle(ready: int, min_ready: int) -> bool:
    """Below waterline: mint the next batch immediately, do not wait mint_interval_seconds."""
    return ready < max(min_ready, 1)


def batch_size(configured: int, ready: int, pool_target: int) -> int:
    """How many tickets this round may mint without overshooting the standing pool."""
    room = max(pause_at(pool_target) - max(int(ready), 0), 0)
    return max(min(int(configured), room), 0)

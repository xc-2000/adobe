from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class HarvesterConfig:
    target_url: str
    internal_token: str
    proxy_file: Path
    proxy_scheme: str
    browser_version: str
    license_key: str | None
    interval_seconds: float
    headless: bool
    geoip: bool
    run_once: bool
    blocked_resource_types: frozenset[str]
    mint_timeout_seconds: float

    @classmethod
    def from_env(cls) -> HarvesterConfig:
        proxy_file = Path(
            os.getenv("ARP_PROXY_FILE", str(PROJECT_ROOT / "secrets" / "arp_proxies.txt"))
        ).expanduser()
        internal_token = os.getenv("ARP_INTERNAL_TOKEN", "dev-only-internal-token")
        if not internal_token:
            raise ValueError("ARP_INTERNAL_TOKEN is required")
        blocked = frozenset(
            item.strip().lower()
            for item in os.getenv("HARVESTER_BLOCK_RESOURCE_TYPES", "").split(",")
            if item.strip()
        )
        allowed_block_types = {"image", "media", "font", "stylesheet"}
        if not blocked.issubset(allowed_block_types):
            raise ValueError("HARVESTER_BLOCK_RESOURCE_TYPES contains an unsupported type")
        return cls(
            target_url=os.getenv("HARVESTER_TARGET", "http://127.0.0.1:18000").rstrip("/"),
            internal_token=internal_token,
            proxy_file=proxy_file,
            proxy_scheme=os.getenv("ARP_PROXY_SCHEME", "http").lower(),
            browser_version=os.getenv(
                "CLOAKBROWSER_VERSION",
                "148.0.7778.215.5",
            ),
            license_key=os.getenv("CLOAKBROWSER_LICENSE_KEY") or None,
            interval_seconds=float(os.getenv("ARP_INTERVAL", "75")),
            headless=_env_bool("HEADLESS", True),
            geoip=_env_bool("CLOAKBROWSER_GEOIP", True),
            run_once=_env_bool("HARVESTER_ONCE", False),
            blocked_resource_types=blocked,
            mint_timeout_seconds=float(os.getenv("ARP_MINT_TIMEOUT", "90")),
        )

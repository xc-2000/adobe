from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Locked. Changing this breaks ARP identity (submit UA/CH must be Chrome 148).
# Open-source fingerprint-chromium release tag, not CloakBrowser's extra .5 suffix.
BROWSER_VERSION = "148.0.7778.215"
DEFAULT_CHROME_BINARY = PROJECT_ROOT / "vendor" / "fingerprint-chromium" / "chrome.exe"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _optional_proxy_file() -> Path | None:
    raw = os.getenv("ARP_PROXY_FILE")
    path = Path(raw).expanduser() if raw else PROJECT_ROOT / "secrets" / "arp_proxies.txt"
    return path if path.is_file() else None


def resolve_chrome_binary(configured: Path) -> Path:
    """Return the fingerprint-chromium 148 executable or raise."""
    path = configured.expanduser()
    if path.is_dir():
        for name in ("chrome.exe", "chrome", "Chromium"):
            candidate = path / name
            if candidate.is_file():
                return candidate
        nested = next(path.rglob("chrome.exe"), None) or next(path.rglob("chrome"), None)
        if nested is not None:
            return nested
    if path.is_file():
        return path
    raise FileNotFoundError(
        f"fingerprint-chromium 148 not found at {path}. "
        "Set ADOBE_CHROME_BINARY or extract the 148 zip to vendor/fingerprint-chromium/"
    )


@dataclass(frozen=True, slots=True)
class HarvesterConfig:
    target_url: str
    internal_token: str
    proxy_file: Path | None
    browser_version: str
    chrome_binary: Path
    license_key: str | None
    headless: bool
    geoip: bool
    run_once: bool
    blocked_resource_types: frozenset[str]
    mint_timeout_seconds: float

    @classmethod
    def from_env(cls) -> HarvesterConfig:
        internal_token = os.getenv("ARP_INTERNAL_TOKEN", "change-me-internal-token")
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
        chrome_raw = os.getenv("ADOBE_CHROME_BINARY") or os.getenv("FINGERPRINT_CHROMIUM")
        chrome_binary = Path(chrome_raw).expanduser() if chrome_raw else DEFAULT_CHROME_BINARY
        return cls(
            target_url=os.getenv("HARVESTER_TARGET", "http://127.0.0.1:18000").rstrip("/"),
            internal_token=internal_token,
            proxy_file=_optional_proxy_file(),
            browser_version=BROWSER_VERSION,
            chrome_binary=chrome_binary,
            license_key=os.getenv("CLOAKBROWSER_LICENSE_KEY") or None,
            headless=_env_bool("HEADLESS", True),
            geoip=_env_bool("CLOAKBROWSER_GEOIP", False),
            run_once=_env_bool("HARVESTER_ONCE", False),
            blocked_resource_types=blocked,
            mint_timeout_seconds=float(os.getenv("ARP_MINT_TIMEOUT", "120")),
        )

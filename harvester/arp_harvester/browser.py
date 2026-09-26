from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from arp_harvester.config import HarvesterConfig, resolve_chrome_binary
from arp_harvester.proxies import parse_proxy, playwright_proxy
from arp_harvester.proxy_relay import AuthProxyRelay
from arp_harvester.reaper import kill_tree, pid_alive

FIREFLY_GENERATE_URL = "https://firefly.adobe.com/generate/image"
SHERLOCK_SDK_URL = "https://commerce.adobe.com/amsterdam/sdk/sherlock.min.js"

_SHERLOCK_WAIT_SECONDS = 15.0
_CLOSE_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("arp_harvester")

_SDK_MINT_JS = r"""
() => new Promise((resolve) => {
  let settled = false;
  const getCH = async () => {
    try {
      return await navigator.userAgentData.getHighEntropyValues([
        "platform", "platformVersion", "fullVersionList",
        "architecture", "bitness", "model", "uaFullVersion"
      ]);
    } catch (e) { return null; }
  };
  const done = async (token) => {
    if (settled) return;
    settled = true;
    resolve({token: token || null, ua: navigator.userAgent, ch: await getCH()});
  };
  const sdk = window.SherlockSdk;
  if (!sdk) { done(null); return; }
  let best = null;
  const timer = setTimeout(() => done(best), 45000);
  try {
    sdk.initAsync({
      clientId: "clio-playground-web",
      prodEnv: true,
      sessionId: crypto.randomUUID(),
      logging: false,
      tokenCallback: (token) => {
        best = token;
        try {
          const decoded = JSON.parse(atob(token));
          if (decoded.sid && decoded.ark && decoded.bfp && decoded.ftr) {
            clearTimeout(timer);
            done(token);
          }
        } catch (e) {}
      },
      errorCallback: () => {},
    });
  } catch (e) {
    clearTimeout(timer);
    done(null);
  }
})
"""


class MintError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MintedArp:
    token: str = field(repr=False)
    proxy_url: str = field(repr=False)
    egress_id: str
    ua: str
    ch: dict[str, Any]
    headers: dict[str, str]
    exit_ip: str | None = None

    def publish_payload(self) -> dict[str, object]:
        return {
            "t": self.token,
            "proxy_url": self.proxy_url,
            "egress_id": self.egress_id,
            "ua": self.ua,
            "ch": self.ch,
            "headers": self.headers,
        }


def _validate_token(token: str) -> None:
    try:
        payload = json.loads(base64.b64decode(token, validate=True))
    except Exception as exc:
        raise MintError("Sherlock returned an invalid ARP token") from exc
    required = {"sid", "ark", "bfp", "ftr"}
    if not isinstance(payload, dict) or any(not payload.get(name) for name in required):
        raise MintError("Sherlock ARP token is missing sid/ark/bfp/ftr")
    ftr = str(payload.get("ftr") or "")
    if "v2_tt" not in ftr:
        raise MintError("ARP ftr is missing v2_tt")


def require_public_ip(value: str | None) -> str:
    raw = (value or "").strip()
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise MintError("minted without proxy egress") from exc
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        raise MintError("minted without proxy egress")
    return str(addr)


def require_matching_egress(lease_ip: str, browser_ip: str) -> str:
    left = require_public_ip(lease_ip)
    right = require_public_ip(browser_ip)
    if left != right:
        raise MintError("browser did not use the lease proxy")
    return right


async def probe_lease_ip(proxy_url: str) -> str:
    """Same credentialed URL the submit worker uses. Fail closed if CONNECT is dead."""
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=20.0) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            payload = response.json()
    except MintError:
        raise
    except Exception as exc:
        raise MintError("lease proxy CONNECT failed") from exc
    ip = payload.get("ip") if isinstance(payload, dict) else None
    return require_public_ip(str(ip or ""))


async def _page_exit_ip(page: Any) -> str:
    try:
        response = await page.goto(
            "https://api.ipify.org?format=json",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        if not response or not response.ok:
            raise MintError("browser proxy egress check failed")
        body = await page.text_content("body")
        payload = json.loads(body or "")
        ip = payload.get("ip") if isinstance(payload, dict) else None
        return require_public_ip(str(ip or ""))
    except MintError:
        raise
    except Exception as exc:
        raise MintError("browser proxy egress check failed") from exc


def overlay_forter_cookie(token: str, cookie_ftr: str) -> str:
    """Sherlock's ``ftr`` is sometimes the short id. Colligo wants the cookie."""
    cookie_ftr = (cookie_ftr or "").strip()
    if not cookie_ftr:
        return token
    try:
        payload = json.loads(base64.b64decode(token, validate=True))
    except Exception as exc:
        raise MintError("Sherlock returned an invalid ARP token") from exc
    if not isinstance(payload, dict):
        raise MintError("Sherlock returned an invalid ARP token")
    current = str(payload.get("ftr") or "")
    if cookie_ftr == current:
        return token
    cookie_better = ("v2_tt" in cookie_ftr and "v2_tt" not in current) or (
        len(cookie_ftr) > len(current)
    )
    if not cookie_better:
        return token
    payload["ftr"] = cookie_ftr
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


async def _forter_cookie(page: Any, timeout_seconds: float = 8.0) -> str:
    """Wait for the document ``forter`` cookie, never log the value."""
    deadline = time.monotonic() + timeout_seconds
    best = ""
    while time.monotonic() < deadline:
        try:
            cookies = await page.context.cookies()
        except Exception:
            cookies = []
        if not cookies:
            try:
                raw = await page.evaluate(
                    "() => (document.cookie.split(';').map(s => s.trim())"
                    ".find(s => s.startsWith('forter=')) || '').slice(7)"
                )
                if isinstance(raw, str) and raw.strip():
                    cookies = [{"name": "forter", "value": raw.strip()}]
            except Exception:
                cookies = []
        for item in cookies:
            if not isinstance(item, dict) or item.get("name") != "forter":
                continue
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            if "v2_tt" in value:
                return value
            if len(value) > len(best):
                best = value
        if best and time.monotonic() + 0.4 >= deadline:
            return best
        await asyncio.sleep(0.4)
    return best


def _build_headers(ua: str, ch: dict[str, Any]) -> dict[str, str]:
    versions = ch.get("fullVersionList")
    if not isinstance(versions, list) or not versions:
        raise MintError("browser did not return fullVersionList")

    compact: list[str] = []
    full: list[str] = []
    for item in versions:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand", ""))
        version = str(item.get("version", ""))
        if not brand or not version:
            continue
        compact.append(f'"{brand}";v="{version.split(".", 1)[0]}"')
        full.append(f'"{brand}";v="{version}"')
    if not compact:
        raise MintError("browser returned no usable Client-Hint brands")

    return {
        "User-Agent": ua,
        "sec-ch-ua": ", ".join(compact),
        "sec-ch-ua-full-version-list": ", ".join(full),
        "sec-ch-ua-platform": f'"{ch.get("platform", "Windows")}"',
        "sec-ch-ua-mobile": "?1" if ch.get("mobile") else "?0",
        "sec-ch-ua-platform-version": f'"{ch.get("platformVersion", "")}"',
        "sec-ch-ua-arch": f'"{ch.get("architecture", "")}"',
        "sec-ch-ua-bitness": f'"{ch.get("bitness", "")}"',
        "sec-ch-ua-model": f'"{ch.get("model", "")}"',
        "sec-ch-ua-full-version": f'"{ch.get("uaFullVersion", "")}"',
    }


def browser_pid(browser: Any) -> int | None:
    """Best-effort PID so the watchdog can kill a hung CloakBrowser tree."""
    for attr in ("pid", "process"):
        value = getattr(browser, attr, None)
        if isinstance(value, int) and value > 0:
            return value
        inner = getattr(value, "pid", None) if value is not None else None
        if isinstance(inner, int) and inner > 0:
            return inner
    for attr in ("_browser", "browser"):
        nested = getattr(browser, attr, None)
        if nested is None:
            continue
        found = browser_pid(nested)
        if found:
            return found
    return None


async def close_browser(browser: Any | None, pid: int | None) -> None:
    if browser is not None:
        closer = getattr(browser, "close", None)
        if closer is not None:
            try:
                await asyncio.wait_for(closer(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("browser.close hung or failed pid=%s", pid)
    if pid and pid_alive(pid):
        kill_tree(pid)


async def wait_sherlock(page: Any, timeout_seconds: float = _SHERLOCK_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if await page.evaluate("() => !!window.SherlockSdk"):
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise MintError("Sherlock SDK not ready")


def _launch_args(seed: int) -> list[str]:
    args = [
        f"--fingerprint={seed}",
        "--fingerprint-platform=windows",
        "--fingerprint-brand=Chrome",
        "--lang=en-US",
        "--accept-lang=en-US,en;q=0.9",
        "--window-size=1366,768",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
    ]
    if sys.platform != "win32":
        args.append("--no-sandbox")
    return args


async def _prepare_page(page: Any, blocked_resource_types: frozenset[str]) -> None:
    if not blocked_resource_types:
        return

    async def block_selected_resources(route: Any) -> None:
        if route.request.resource_type in blocked_resource_types:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_selected_resources)


async def _mint_on_page(
    page: Any, *, proxy_url: str, egress_id: str, lease_ip: str
) -> MintedArp:
    exit_ip = require_matching_egress(lease_ip, await _page_exit_ip(page))

    await page.goto(
        FIREFLY_GENERATE_URL,
        wait_until="domcontentloaded",
        timeout=30000,
    )
    try:
        await page.add_script_tag(url=SHERLOCK_SDK_URL)
    except Exception as exc:
        logger.warning("sherlock sdk tag failed error=%s", type(exc).__name__)
    await wait_sherlock(page)
    result = await page.evaluate(_SDK_MINT_JS)
    if not isinstance(result, dict) or not isinstance(result.get("token"), str):
        raise MintError("Sherlock SDK returned no ARP token")

    token = result["token"]
    cookie_ftr = await _forter_cookie(page)
    token = overlay_forter_cookie(token, cookie_ftr)
    ua = result.get("ua")
    ch = result.get("ch")
    if not isinstance(ua, str) or "Chrome/148." not in ua:
        raise MintError("minting browser identity is not Chrome 148")
    if not isinstance(ch, dict):
        raise MintError("minting browser returned no Client Hints")
    _validate_token(token)
    return MintedArp(
        token=token,
        proxy_url=proxy_url,
        egress_id=egress_id,
        ua=ua,
        ch=ch,
        headers=_build_headers(ua, ch),
        exit_ip=exit_ip,
    )


class HotMintSession:
    """One Chrome process. Relaunch per residential egress (proxy is process-level)."""

    def __init__(self, config: HarvesterConfig) -> None:
        self._config = config
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._relay: AuthProxyRelay | None = None
        self._pid: int | None = None
        self._proxy_url: str | None = None

    async def _close_relay(self) -> None:
        relay = self._relay
        self._relay = None
        if relay is not None:
            await relay.close()

    async def _ensure_playwright(self) -> Any:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return self._playwright

    async def _reset_browser(self) -> None:
        browser, pid = self._browser, self._pid
        self._browser = None
        self._pid = None
        self._proxy_url = None
        await close_browser(browser, pid)

    async def _launch(self, proxy_url: str, license_key: str | None) -> None:
        del license_key
        await self._reset_browser()
        await self._close_relay()
        seed = random.SystemRandom().randint(10000, 99999)
        try:
            chrome = resolve_chrome_binary(self._config.chrome_binary)
        except FileNotFoundError as exc:
            raise MintError(str(exc)) from exc
        playwright = await self._ensure_playwright()
        record = parse_proxy(proxy_url, default_scheme="http")
        launch_args = _launch_args(seed)
        launch_kwargs: dict[str, Any] = {
            "executable_path": str(chrome),
            "headless": self._config.headless,
            "ignore_default_args": ["--enable-automation"],
        }
        if record.scheme in {"socks5", "socks5h"}:
            launch_kwargs["args"] = launch_args
            launch_kwargs["proxy"] = playwright_proxy(proxy_url)
        else:
            self._relay = await AuthProxyRelay.start(proxy_url)
            launch_kwargs["args"] = launch_args + [f"--proxy-server={self._relay.local_url}"]
        self._browser = await playwright.chromium.launch(**launch_kwargs)
        self._pid = browser_pid(self._browser)
        self._proxy_url = proxy_url

    async def _open_page(self, proxy_url: str, license_key: str | None) -> tuple[Any | None, Any]:
        if self._browser is None or proxy_url != self._proxy_url:
            await self._launch(proxy_url, license_key)
        assert self._browser is not None
        return None, await self._browser.new_page()

    async def mint(
        self,
        *,
        proxy_url: str,
        egress_id: str,
        license_key: str | None = None,
    ) -> MintedArp:
        context = None
        page = None
        try:
            async with asyncio.timeout(self._config.mint_timeout_seconds):
                lease_ip = await probe_lease_ip(proxy_url)
                context, page = await self._open_page(proxy_url, license_key)
                await _prepare_page(page, self._config.blocked_resource_types)
                return await _mint_on_page(
                    page, proxy_url=proxy_url, egress_id=egress_id, lease_ip=lease_ip
                )
        except Exception:
            await self.close()
            raise
        finally:
            if page is not None:
                closer = getattr(page, "close", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:
                        pass
            if context is not None:
                closer = getattr(context, "close", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:
                        pass

    async def close(self) -> None:
        await self._reset_browser()
        await self._close_relay()
        playwright = self._playwright
        self._playwright = None
        if playwright is None:
            return
        stopper = getattr(playwright, "stop", None)
        if stopper is None:
            return
        try:
            await asyncio.wait_for(stopper(), timeout=_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("playwright.stop hung or failed")


async def mint_arp(
    config: HarvesterConfig,
    *,
    proxy_url: str,
    egress_id: str,
    license_key: str | None = None,
) -> MintedArp:
    session = HotMintSession(config)
    try:
        return await session.mint(proxy_url=proxy_url, egress_id=egress_id, license_key=license_key)
    finally:
        await session.close()

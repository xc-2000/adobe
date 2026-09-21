from __future__ import annotations

import asyncio
import base64
import json
import random
from dataclasses import dataclass, field
from typing import Any

from cloakbrowser import launch_async

from arp_harvester.config import HarvesterConfig
from arp_harvester.proxies import ProxyRecord

FIREFLY_GENERATE_URL = "https://firefly.adobe.com/generate/image"
SHERLOCK_SDK_URL = "https://commerce.adobe.com/amsterdam/sdk/sherlock.min.js"

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
  const timer = setTimeout(() => done(best), 60000);
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


async def mint_arp(
    config: HarvesterConfig,
    proxy: ProxyRecord,
) -> MintedArp:
    seed = random.SystemRandom().randint(10000, 99999)
    launch_kwargs: dict[str, Any] = {
        "browser_version": config.browser_version,
        "proxy": proxy.url,
        "geoip": config.geoip,
        "headless": config.headless,
        "args": [
            f"--fingerprint={seed}",
            "--fingerprint-platform=windows",
            "--fingerprint-brand=Chrome",
            "--lang=en-US",
            "--accept-lang=en-US,en;q=0.9",
            "--window-size=1366,768",
            "--fingerprint-webrtc-ip=auto",
        ],
    }
    if config.license_key:
        launch_kwargs["license_key"] = config.license_key

    browser = await launch_async(**launch_kwargs)
    try:
        page = await browser.new_page()
        if config.blocked_resource_types:
            async def block_selected_resources(route: Any) -> None:
                if route.request.resource_type in config.blocked_resource_types:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_selected_resources)
        exit_ip: str | None = None
        try:
            response = await page.goto(
                "https://api.ipify.org?format=json",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if response and response.ok:
                body = await page.text_content("body")
                if body:
                    exit_ip = str(json.loads(body).get("ip") or "") or None
        except Exception:
            exit_ip = None

        await page.goto(
            FIREFLY_GENERATE_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.add_script_tag(url=SHERLOCK_SDK_URL)
        async with asyncio.timeout(config.mint_timeout_seconds):
            result = await page.evaluate(_SDK_MINT_JS)
        if not isinstance(result, dict) or not isinstance(result.get("token"), str):
            raise MintError("Sherlock SDK returned no ARP token")

        token = result["token"]
        ua = result.get("ua")
        ch = result.get("ch")
        if not isinstance(ua, str) or "Chrome/148." not in ua:
            raise MintError("minting browser identity is not Chrome 148")
        if not isinstance(ch, dict):
            raise MintError("minting browser returned no Client Hints")
        _validate_token(token)
        headers = _build_headers(ua, ch)
        return MintedArp(
            token=token,
            proxy_url=proxy.url,
            egress_id=proxy.egress_id,
            ua=ua,
            ch=ch,
            headers=headers,
            exit_ip=exit_ip,
        )
    finally:
        await browser.close()

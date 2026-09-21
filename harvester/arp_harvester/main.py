from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from arp_harvester.browser import MintedArp, mint_arp
from arp_harvester.config import HarvesterConfig
from arp_harvester.proxies import ProxyRecord, load_proxy_file

logger = logging.getLogger("arp_harvester")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _masked_ip(value: str | None) -> str:
    if not value:
        return "unknown"
    if ":" in value:
        return value.split(":", 1)[0] + ":*"
    parts = value.split(".")
    return ".".join(parts[:3] + ["*"]) if len(parts) == 4 else "unknown"


async def _publish(
    client: httpx.AsyncClient,
    config: HarvesterConfig,
    minted: MintedArp,
) -> str:
    response = await client.post(
        f"{config.target_url}/admin-api/adobe/arp",
        headers={"X-Internal-Token": config.internal_token},
        json={"tokens": [minted.publish_payload()]},
    )
    response.raise_for_status()
    payload = response.json()
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("ARP service returned no bundle id")
    return str(ids[0])


async def _run_cycle(
    client: httpx.AsyncClient,
    config: HarvesterConfig,
    proxy: ProxyRecord,
) -> None:
    minted = await mint_arp(config, proxy)
    bundle_id = await _publish(client, config, minted)
    logger.info(
        "published arp id=%s egress=%s exit_ip=%s browser=148",
        bundle_id,
        proxy.egress_id,
        _masked_ip(minted.exit_ip),
    )


async def main() -> None:
    _configure_logging()
    config = HarvesterConfig.from_env()
    proxies = load_proxy_file(config.proxy_file, default_scheme=config.proxy_scheme)
    logger.info(
        "harvester ready proxies=%d browser=%s interval=%ss",
        len(proxies),
        config.browser_version,
        config.interval_seconds,
    )

    backoff = config.interval_seconds
    cycle = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            proxy = proxies[cycle % len(proxies)]
            try:
                await _run_cycle(client, config, proxy)
                backoff = config.interval_seconds
            except Exception as exc:
                logger.error(
                    "mint cycle failed egress=%s error=%s",
                    proxy.egress_id,
                    type(exc).__name__,
                )
                backoff = min(max(backoff * 1.5, config.interval_seconds), 300)
                if config.run_once:
                    raise
            cycle += 1
            if config.run_once:
                return
            await asyncio.sleep(backoff)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()

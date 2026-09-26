from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from arp_harvester.browser import mint_arp
from arp_harvester.config import HarvesterConfig
from arp_harvester.panel import Lease
from arp_harvester.proxies import load_proxy_file
from arp_harvester.supervisor import Supervisor

logger = logging.getLogger("arp_harvester")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _file_leases(config: HarvesterConfig) -> list[Lease]:
    if config.proxy_file is None:
        return []
    records = load_proxy_file(config.proxy_file, default_scheme="http")
    return [Lease(egress_id=item.egress_id, proxy_url=item.url, source="file") for item in records]


async def main() -> None:
    _configure_logging()
    config = HarvesterConfig.from_env()
    file_leases = _file_leases(config)
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        supervisor = Supervisor(client, config, file_leases, mint_arp)
        await supervisor.run()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()

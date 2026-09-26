"""HTTP client for the panel admin-api. No secrets are logged."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any

import httpx

from arp_harvester.config import HarvesterConfig


@dataclass(frozen=True, slots=True)
class Lease:
    egress_id: str
    proxy_url: str = field(repr=False)
    source: str


@dataclass(slots=True)
class Bootstrap:
    version: int
    mint_browser_count: int
    mint_interval_seconds: float
    arp_ttl_seconds: int
    arp_min_ready: int
    arp_pool_target: int
    arp_mint_batch: int
    proxy_ready: bool
    license_key: str | None


def headers(config: HarvesterConfig) -> dict[str, str]:
    return {"X-Internal-Token": config.internal_token}


def parse_bootstrap(payload: dict[str, Any]) -> Bootstrap:
    min_ready = max(int(payload.get("arp_min_ready") or 1), 1)
    target = max(int(payload.get("arp_pool_target") or 8), min_ready)
    return Bootstrap(
        version=int(payload.get("version") or 0),
        mint_browser_count=max(min(int(payload.get("mint_browser_count") or 1), 8), 1),
        mint_interval_seconds=float(payload.get("mint_interval_seconds") or 10),
        arp_ttl_seconds=max(int(payload.get("arp_ttl_seconds") or 120), 30),
        arp_min_ready=min_ready,
        arp_pool_target=min(target, 20),
        arp_mint_batch=max(min(int(payload.get("arp_mint_batch") or 8), 20), 1),
        proxy_ready=bool(payload.get("proxy_ready")),
        license_key=payload.get("cloakbrowser_license_key") or None,
    )


async def fetch_bootstrap(client: httpx.AsyncClient, config: HarvesterConfig) -> Bootstrap:
    response = await client.get(
        f"{config.target_url}/admin-api/adobe/harvester/bootstrap",
        headers=headers(config),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("bootstrap returned invalid JSON")
    return parse_bootstrap(payload)


async def fetch_ready(client: httpx.AsyncClient, config: HarvesterConfig) -> int:
    response = await client.get(
        f"{config.target_url}/admin-api/adobe/arp",
        headers=headers(config),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return 0
    return int(payload.get("ready") or 0)


async def lease_from_panel(client: httpx.AsyncClient, config: HarvesterConfig) -> Lease:
    response = await client.post(
        f"{config.target_url}/admin-api/adobe/harvester/lease",
        headers=headers(config),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("lease returned invalid JSON")
    proxy_url = payload.get("proxy_url")
    egress_id = payload.get("egress_id")
    if not isinstance(proxy_url, str) or not isinstance(egress_id, str):
        raise RuntimeError("lease missing proxy_url or egress_id")
    return Lease(egress_id=egress_id, proxy_url=proxy_url, source="panel")


async def publish(
    client: httpx.AsyncClient,
    config: HarvesterConfig,
    payload: dict[str, object],
) -> str:
    response = await client.post(
        f"{config.target_url}/admin-api/adobe/arp",
        headers=headers(config),
        json={"tokens": [payload]},
    )
    response.raise_for_status()
    body = response.json()
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("ARP service returned no bundle id")
    return str(ids[0])


async def next_lease(
    client: httpx.AsyncClient,
    config: HarvesterConfig,
    boot: Bootstrap,
    file_cycle: count,
    file_leases: list[Lease],
) -> Lease:
    if boot.proxy_ready:
        return await lease_from_panel(client, config)
    if not file_leases:
        raise RuntimeError("panel proxy is not configured and no ARP_PROXY_FILE override")
    return file_leases[next(file_cycle) % len(file_leases)]

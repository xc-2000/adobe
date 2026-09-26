"""Never-die mint supervisor.

Keeps ``mint_browser_count`` hot workers (1–2 is enough for 20-way submit).
Each worker mints a BATCH into a standing pool, then idles. One hung Chrome
cannot take down generation: the session relaunches, and a dead task is
replaced on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from arp_harvester.browser import HotMintSession, MintedArp, MintError, mint_arp
from arp_harvester.config import HarvesterConfig
from arp_harvester.panel import Bootstrap, Lease, fetch_bootstrap, fetch_ready, next_lease, publish
from arp_harvester.rules import (
    batch_size,
    is_proxy_connect_failure,
    should_pause_mint,
    should_skip_idle,
)

logger = logging.getLogger("arp_harvester")

MintFn = Callable[..., Awaitable[MintedArp]]
SUPERVISOR_TICK_SECONDS = 3.0
CONNECT_PAUSE_SECONDS = 45.0


class MintCircuit:
    """Shared pause so failed CONNECT does not relaunch five Chromes in a tight loop."""

    def __init__(self) -> None:
        self._until = 0.0

    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic())

    def trip(self, seconds: float) -> None:
        self._until = max(self._until, time.monotonic() + seconds)


def _masked_ip(value: str | None) -> str:
    if not value:
        return "unknown"
    if ":" in value:
        return value.split(":", 1)[0] + ":*"
    parts = value.split(".")
    return ".".join(parts[:3] + ["*"]) if len(parts) == 4 else "unknown"


async def browser_loop(
    client: httpx.AsyncClient,
    config: HarvesterConfig,
    worker_id: int,
    file_leases: list[Lease],
    mint_fn: MintFn,
    circuit: MintCircuit | None = None,
) -> None:
    from itertools import count

    file_cycle = count()
    backoff = 8.0
    boot: Bootstrap | None = None
    hot = HotMintSession(config) if mint_fn is mint_arp else None
    gate = circuit or MintCircuit()
    try:
        while True:
            paused = gate.remaining()
            if paused > 0:
                logger.warning(
                    "mint paused after proxy CONNECT failure worker=%s sleep=%.0fs",
                    worker_id,
                    paused,
                )
                await asyncio.sleep(paused)
                continue
            try:
                boot = await fetch_bootstrap(client, config)
            except Exception as exc:
                logger.error("bootstrap failed worker=%s error=%s", worker_id, type(exc).__name__)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 1.5, 300)
                continue

            try:
                ready = await fetch_ready(client, config)
            except Exception as exc:
                logger.error("pool health failed worker=%s error=%s", worker_id, type(exc).__name__)
                await asyncio.sleep(boot.mint_interval_seconds)
                continue

            if should_pause_mint(ready, boot.arp_pool_target, boot.arp_min_ready):
                logger.info(
                    "arp pool standing ready=%s target=%s min=%s ttl=%s worker=%s",
                    ready,
                    boot.arp_pool_target,
                    boot.arp_min_ready,
                    boot.arp_ttl_seconds,
                    worker_id,
                )
                if config.run_once:
                    return
                await asyncio.sleep(boot.mint_interval_seconds)
                continue

            minted_in_round = 0
            want = batch_size(boot.arp_mint_batch, ready, boot.arp_pool_target)
            if config.run_once:
                want = max(want, 1)
            lease: Lease | None = None
            try:
                for _ in range(want):
                    lease = await next_lease(client, config, boot, file_cycle, file_leases)
                    if not (lease.proxy_url or "").strip():
                        raise MintError("lease missing proxy")
                    if hot is not None:
                        minted = await hot.mint(
                            proxy_url=lease.proxy_url,
                            egress_id=lease.egress_id,
                            license_key=boot.license_key or config.license_key,
                        )
                    else:
                        minted = await mint_fn(
                            config,
                            proxy_url=lease.proxy_url,
                            egress_id=lease.egress_id,
                            license_key=boot.license_key or config.license_key,
                        )
                    if not minted.exit_ip:
                        raise MintError("minted without proxy egress")
                    bundle_id = await publish(client, config, minted.publish_payload())
                    minted_in_round += 1
                    ready += 1
                    logger.info(
                        "published arp id=%s egress=%s exit_ip=%s source=%s browser=148 "
                        "worker=%s batch=%s/%s ready=%s",
                        bundle_id,
                        lease.egress_id,
                        _masked_ip(minted.exit_ip),
                        lease.source,
                        worker_id,
                        minted_in_round,
                        want,
                        ready,
                    )
                    if should_pause_mint(ready, boot.arp_pool_target, boot.arp_min_ready):
                        break
                    if config.run_once:
                        return
                backoff = boot.mint_interval_seconds
                if config.run_once:
                    return
                if should_skip_idle(ready, boot.arp_min_ready):
                    continue
                await asyncio.sleep(boot.mint_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"[:400]
                logger.error(
                    "mint cycle failed egress=%s worker=%s error=%s",
                    lease.egress_id if lease else "none",
                    worker_id,
                    detail,
                )
                backoff = min(max(backoff * 1.5, boot.mint_interval_seconds), 300)
                if is_proxy_connect_failure(exc):
                    gate.trip(CONNECT_PAUSE_SECONDS)
                    backoff = max(backoff, CONNECT_PAUSE_SECONDS)
                if config.run_once:
                    raise
                try:
                    await fetch_bootstrap(client, config)
                except Exception as boot_exc:
                    logger.error(
                        "bootstrap heartbeat failed worker=%s error=%s",
                        worker_id,
                        type(boot_exc).__name__,
                    )
                await asyncio.sleep(max(backoff, gate.remaining()))
    finally:
        if hot is not None:
            await hot.close()


class Supervisor:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: HarvesterConfig,
        file_leases: list[Lease],
        mint_fn: MintFn,
    ) -> None:
        self._client = client
        self._config = config
        self._file_leases = file_leases
        self._mint_fn = mint_fn
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._desired = 0
        self._circuit = MintCircuit()

    async def run(self) -> None:
        try:
            while True:
                await self._tick()
                if self._config.run_once:
                    if self._workers:
                        await asyncio.gather(*self._workers.values(), return_exceptions=True)
                    return
                await asyncio.sleep(SUPERVISOR_TICK_SECONDS)
        finally:
            await self.shutdown()

    async def _tick(self) -> None:
        try:
            boot = await fetch_bootstrap(self._client, self._config)
        except Exception as exc:
            logger.error("supervisor heartbeat failed error=%s", type(exc).__name__)
            return
        desired = 1 if self._config.run_once else boot.mint_browser_count
        if desired != self._desired:
            logger.info(
                "harvester watchdog browsers=%s batch=%s interval=%ss target=%s ttl=%ss proxy_ready=%s",
                desired,
                boot.arp_mint_batch,
                boot.mint_interval_seconds,
                boot.arp_pool_target,
                boot.arp_ttl_seconds,
                boot.proxy_ready,
            )
            self._desired = desired
        await self._apply(desired)

    async def _apply(self, desired: int) -> None:
        for worker_id in range(desired):
            task = self._workers.get(worker_id)
            if task is not None and not task.done():
                continue
            if task is not None and task.done():
                reason: str = "done"
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        reason = type(exc).__name__
                logger.error("mint worker died worker=%s error=%s — restarting", worker_id, reason)
            self._workers[worker_id] = asyncio.create_task(
                browser_loop(
                    self._client,
                    self._config,
                    worker_id,
                    self._file_leases,
                    self._mint_fn,
                    self._circuit,
                ),
                name=f"arp-mint-{worker_id}",
            )
        extra = [worker_id for worker_id in self._workers if worker_id >= desired]
        for worker_id in extra:
            self._workers[worker_id].cancel()
        if extra:
            await asyncio.gather(*(self._workers[i] for i in extra), return_exceptions=True)
            for worker_id in extra:
                self._workers.pop(worker_id, None)

    async def shutdown(self) -> None:
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()

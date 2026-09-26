import asyncio
from pathlib import Path
from typing import Any

import pytest

from arp_harvester.config import HarvesterConfig
from arp_harvester.panel import Bootstrap
from arp_harvester.supervisor import Supervisor


def _config() -> HarvesterConfig:
    return HarvesterConfig(
        target_url="http://127.0.0.1:9",
        internal_token="test-internal-token",
        proxy_file=None,
        browser_version="148.0.7778.215",
        chrome_binary=Path("chrome.exe"),
        license_key=None,
        headless=True,
        geoip=False,
        run_once=False,
        blocked_resource_types=frozenset(),
        mint_timeout_seconds=5,
    )


def _boot(browsers: int) -> Bootstrap:
    return Bootstrap(
        version=1,
        mint_browser_count=browsers,
        mint_interval_seconds=0.05,
        arp_ttl_seconds=120,
        arp_min_ready=3,
        arp_pool_target=8,
        arp_mint_batch=8,
        proxy_ready=True,
        license_key=None,
    )


@pytest.mark.asyncio
async def test_supervisor_scales_workers_and_restarts_dead(monkeypatch: Any) -> None:
    state = {"browsers": 2}

    async def fake_boot(*_a: object, **_k: object) -> Bootstrap:
        return _boot(state["browsers"])

    async def fake_ready(*_a: object, **_k: object) -> int:
        return 99

    monkeypatch.setattr("arp_harvester.supervisor.fetch_bootstrap", fake_boot)
    monkeypatch.setattr("arp_harvester.supervisor.fetch_ready", fake_ready)

    async def unused_mint(*_a: object, **_k: object) -> None:
        raise AssertionError("waterline pause should not mint")

    supervisor = Supervisor(object(), _config(), [], unused_mint)  # type: ignore[arg-type]
    await supervisor._tick()
    assert sorted(supervisor._workers) == [0, 1]
    first = supervisor._workers[0]

    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await supervisor._apply(2)
    assert supervisor._workers[0] is not first
    assert not supervisor._workers[0].done()

    state["browsers"] = 1
    await supervisor._tick()
    assert list(supervisor._workers) == [0]
    await supervisor.shutdown()
    assert supervisor._workers == {}

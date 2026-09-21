import base64
import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.arp.models import ArpPublishItem
from app.arp.store import ArpStore


def make_item() -> ArpPublishItem:
    token = base64.b64encode(
        json.dumps({"sid": "sid", "ark": "ark", "bfp": "bfp", "ftr": "full_forter"}).encode()
    ).decode()
    return ArpPublishItem.model_validate(
        {
            "t": token,
            "proxy_url": "socks5h://user:password@proxy.example:1080",
            "egress_id": "residential-1",
            "ua": "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36",
            "ch": {
                "platform": "Windows",
                "platformVersion": "15.0.0",
                "fullVersionList": [{"brand": "Google Chrome", "version": "148.0.0.0"}],
                "architecture": "x86",
                "bitness": "64",
            },
        }
    )


@pytest_asyncio.fixture
async def store() -> AsyncIterator[ArpStore]:
    redis = Redis.from_url(
        os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    try:
        await redis.ping()
    except Exception:
        await redis.aclose()
        pytest.skip("Redis is not available")

    prefix = f"test:arp:{uuid4().hex}"
    instance = ArpStore(redis, key_prefix=prefix, ttl_seconds=30, max_uses=2)
    yield instance
    keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


@pytest.mark.asyncio
async def test_publish_checkout_and_max_uses_are_atomic(store: ArpStore) -> None:
    ids = await store.publish([make_item()])

    first = await store.checkout()
    second = await store.checkout()
    exhausted = await store.checkout()

    assert len(ids) == 1
    assert first is not None and first.id == ids[0] and first.uses == 1
    assert second is not None and second.id == ids[0] and second.uses == 2
    assert exhausted is None


@pytest.mark.asyncio
async def test_invalidate_removes_bundle_from_ready_pool(store: ArpStore) -> None:
    bundle_id = (await store.publish([make_item()]))[0]

    removed = await store.invalidate(bundle_id)

    assert removed is True
    assert await store.checkout() is None


@pytest.mark.asyncio
async def test_health_returns_only_non_sensitive_pool_metadata(store: ArpStore) -> None:
    await store.publish([make_item()])

    health = await store.health()

    assert health["present"] is True
    assert health["ready"] == 1
    assert health["egress"] == {"residential-1": 1}
    assert "proxy_url" not in health
    assert "token" not in health

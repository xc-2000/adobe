from __future__ import annotations

import time
from collections import Counter
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

from app.arp.models import ArpBundle, ArpPublishItem

_PUBLISH_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
return 1
"""

_CHECKOUT_SCRIPT = """
local ready = KEYS[1]
local item_prefix = ARGV[1]
local now = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', ready, '-inf', now)
local ids = redis.call('ZREVRANGE', ready, 0, 49)
for _, id in ipairs(ids) do
  local item_key = item_prefix .. id
  local raw = redis.call('GET', item_key)
  if raw then
    local item = cjson.decode(raw)
    if tonumber(item.expires_at) > now and tonumber(item.uses) < tonumber(item.max_uses) then
      item.uses = tonumber(item.uses) + 1
      local ttl = redis.call('PTTL', item_key)
      if ttl > 0 then
        redis.call('SET', item_key, cjson.encode(item), 'PX', ttl)
      end
      if tonumber(item.uses) >= tonumber(item.max_uses) then
        redis.call('ZREM', ready, id)
      end
      return cjson.encode(item)
    end
  end
  redis.call('ZREM', ready, id)
end
return nil
"""

_INVALIDATE_SCRIPT = """
local removed_item = redis.call('DEL', KEYS[1])
local removed_ready = redis.call('ZREM', KEYS[2], ARGV[1])
return removed_item + removed_ready
"""


class ArpStore:
    def __init__(
        self,
        redis: Redis,
        *,
        key_prefix: str,
        ttl_seconds: int,
        max_uses: int,
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix.rstrip(":")
        self._ttl_seconds = ttl_seconds
        self._max_uses = max_uses

    @property
    def _ready_key(self) -> str:
        return f"{self._prefix}:ready"

    @property
    def _item_prefix(self) -> str:
        return f"{self._prefix}:item:"

    def _item_key(self, bundle_id: str) -> str:
        return f"{self._item_prefix}{bundle_id}"

    async def publish(self, items: list[ArpPublishItem]) -> list[str]:
        bundles = [
            ArpBundle.from_publish_item(
                item,
                ttl_seconds=self._ttl_seconds,
                max_uses=self._max_uses,
            )
            for item in items
        ]
        async with self._redis.pipeline(transaction=True) as pipeline:
            for bundle in bundles:
                pipeline.eval(
                    _PUBLISH_SCRIPT,
                    2,
                    self._item_key(bundle.id),
                    self._ready_key,
                    bundle.to_storage_json(),
                    str(self._ttl_seconds * 1000),
                    str(bundle.expires_at),
                    bundle.id,
                )
            await pipeline.execute()
        return [bundle.id for bundle in bundles]

    async def checkout(self) -> ArpBundle | None:
        raw = await cast(
            Awaitable[Any],
            self._redis.eval(
                _CHECKOUT_SCRIPT,
                1,
                self._ready_key,
                self._item_prefix,
                str(int(time.time())),
            ),
        )
        if raw is None:
            return None
        return ArpBundle.from_storage_json(cast(str | bytes, raw))

    async def invalidate(self, bundle_id: str) -> bool:
        removed = await cast(
            Awaitable[Any],
            self._redis.eval(
                _INVALIDATE_SCRIPT,
                2,
                self._item_key(bundle_id),
                self._ready_key,
                bundle_id,
            ),
        )
        return bool(removed)

    async def health(self) -> dict[str, Any]:
        now = int(time.time())
        await self._redis.zremrangebyscore(self._ready_key, "-inf", now)
        ids = await self._redis.zrange(self._ready_key, 0, -1)
        if not ids:
            return {"present": False, "ready": 0, "oldest_age_sec": None, "egress": {}}

        raw_items = await self._redis.mget([self._item_key(str(item)) for item in ids])
        bundles: list[ArpBundle] = []
        dangling: list[str] = []
        for bundle_id, raw in zip(ids, raw_items, strict=True):
            if raw is None:
                dangling.append(str(bundle_id))
                continue
            bundles.append(ArpBundle.from_storage_json(cast(str | bytes, raw)))
        if dangling:
            await self._redis.zrem(self._ready_key, *dangling)

        egress = Counter(bundle.egress_id for bundle in bundles)
        oldest = min((bundle.minted_at for bundle in bundles), default=now)
        return {
            "present": bool(bundles),
            "ready": len(bundles),
            "oldest_age_sec": max(0, now - oldest) if bundles else None,
            "egress": dict(egress),
        }

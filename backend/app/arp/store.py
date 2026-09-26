from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis

from app.arp.models import ArpBundle, ArpPublishItem, cap_arp_max_uses, decode_arp_token

_PUBLISH_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
return 1
"""

_CHECKOUT_SCRIPT = """
local ready = KEYS[1]
local rr_key = KEYS[2]
local item_prefix = ARGV[1]
local now = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', ready, '-inf', now)
local ids = redis.call('ZRANGE', ready, 0, -1)
local n = #ids
if n == 0 then
  redis.call('DEL', rr_key)
  return nil
end
local start = tonumber(redis.call('GET', rr_key) or '0') % n
for i = 0, n - 1 do
  local idx = (start + i) % n
  local id = ids[idx + 1]
  local item_key = item_prefix .. id
  local raw = redis.call('GET', item_key)
  if raw then
    local item = cjson.decode(raw)
    if tonumber(item.expires_at) > now then
      local max_uses = tonumber(item.max_uses)
      if cap ~= nil and max_uses > cap then
        max_uses = cap
        item.max_uses = cap
      end
      if tonumber(item.uses) < max_uses then
        item.uses = tonumber(item.uses) + 1
        local ttl = redis.call('PTTL', item_key)
        if ttl > 0 then
          redis.call('SET', item_key, cjson.encode(item), 'PX', ttl)
        end
        if tonumber(item.uses) >= max_uses then
          redis.call('ZREM', ready, id)
        end
        redis.call('SET', rr_key, (idx + 1) % n)
        return cjson.encode(item)
      end
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
        self._max_uses = cap_arp_max_uses(max_uses)

    @property
    def _ready_key(self) -> str:
        return f"{self._prefix}:ready"

    @property
    def _rr_key(self) -> str:
        return f"{self._prefix}:rr"

    @property
    def _item_prefix(self) -> str:
        return f"{self._prefix}:item:"

    def _item_key(self, bundle_id: str) -> str:
        return f"{self._item_prefix}{bundle_id}"

    @property
    def _events_key(self) -> str:
        return f"{self._prefix}:events"

    @property
    def _last_key(self) -> str:
        return f"{self._prefix}:last_publish"

    @property
    def _heartbeat_key(self) -> str:
        return f"{self._prefix}:harvester:heartbeat"

    @staticmethod
    def _iso(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()

    def _public_ticket(self, bundle: ArpBundle, now: int) -> dict[str, Any]:
        return {
            "id": bundle.id,
            "egress_id": bundle.egress_id,
            "minted_at": ArpStore._iso(bundle.minted_at),
            "expires_at": ArpStore._iso(bundle.expires_at),
            "age_sec": max(0, now - bundle.minted_at),
            "ttl_left_sec": max(0, bundle.expires_at - now),
            "uses": bundle.uses,
            "max_uses": min(bundle.max_uses, self._max_uses),
            "chrome_148": "Chrome/148." in bundle.ua,
        }

    @staticmethod
    def _admitted(bundle: ArpBundle) -> bool:
        try:
            decode_arp_token(bundle.token.get_secret_value())
        except ValueError:
            return False
        return True

    @staticmethod
    def _event_payload(bundle: ArpBundle) -> str:
        return json.dumps(
            {
                "ts": ArpStore._iso(bundle.minted_at),
                "type": "published",
                "id": bundle.id,
                "egress_id": bundle.egress_id,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )

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
            last_event = ""
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
                last_event = self._event_payload(bundle)
                pipeline.lpush(self._events_key, last_event)
            if last_event:
                pipeline.ltrim(self._events_key, 0, 49)
                pipeline.set(self._last_key, last_event)
            await pipeline.execute()
        return [bundle.id for bundle in bundles]

    async def checkout(self) -> ArpBundle | None:
        for _ in range(32):
            raw = await cast(
                Awaitable[Any],
                self._redis.eval(
                    _CHECKOUT_SCRIPT,
                    2,
                    self._ready_key,
                    self._rr_key,
                    self._item_prefix,
                    str(int(time.time())),
                    str(self._max_uses),
                ),
            )
            if raw is None:
                return None
            bundle = ArpBundle.from_storage_json(cast(str | bytes, raw))
            if self._admitted(bundle):
                return bundle
            await self.invalidate(bundle.id)
        return None

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

    async def _ready_bundles(self) -> list[ArpBundle]:
        now = int(time.time())
        await self._redis.zremrangebyscore(self._ready_key, "-inf", now)
        ids = await self._redis.zrange(self._ready_key, 0, -1)
        if not ids:
            return []
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
        kept: list[ArpBundle] = []
        for bundle in bundles:
            if self._admitted(bundle):
                kept.append(bundle)
            else:
                await self.invalidate(bundle.id)
        return kept

    async def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        raw = await self._redis.lrange(self._events_key, 0, max(0, limit - 1))
        events: list[dict[str, Any]] = []
        for item in raw:
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            events.append(
                {
                    "ts": str(parsed.get("ts") or ""),
                    "type": str(parsed.get("type") or "published"),
                    "id": str(parsed.get("id") or ""),
                    "egress_id": str(parsed.get("egress_id") or ""),
                }
            )
        return events

    async def touch_harvester(self, ttl_seconds: int) -> None:
        ttl = max(int(ttl_seconds), 30)
        await self._redis.set(self._heartbeat_key, str(int(time.time())), ex=ttl)

    async def harvester_seen_at(self) -> int | None:
        raw = await self._redis.get(self._heartbeat_key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def last_publish(self) -> dict[str, Any] | None:
        raw = await self._redis.get(self._last_key)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return {
            "ts": str(parsed.get("ts") or ""),
            "id": str(parsed.get("id") or ""),
            "egress_id": str(parsed.get("egress_id") or ""),
        }

    async def health(self) -> dict[str, Any]:
        now = int(time.time())
        bundles = await self._ready_bundles()
        if not bundles:
            return {"present": False, "ready": 0, "oldest_age_sec": None, "egress": {}}
        egress = Counter(bundle.egress_id for bundle in bundles)
        oldest = min((bundle.minted_at for bundle in bundles), default=now)
        return {
            "present": True,
            "ready": len(bundles),
            "oldest_age_sec": max(0, now - oldest),
            "egress": dict(egress),
        }

    async def snapshot(self) -> dict[str, Any]:
        now = int(time.time())
        bundles = await self._ready_bundles()
        tickets = [self._public_ticket(bundle, now) for bundle in bundles]
        tickets.sort(key=lambda ticket: str(ticket.get("minted_at") or ""), reverse=True)
        egress = Counter(bundle.egress_id for bundle in bundles)
        oldest = min((bundle.minted_at for bundle in bundles), default=now)
        events = await self.recent_events(20)
        if not events:
            events = [
                {
                    "ts": ticket["minted_at"],
                    "type": "pooled",
                    "id": ticket["id"],
                    "egress_id": ticket["egress_id"],
                }
                for ticket in tickets
            ]
        last = await self.last_publish()
        return {
            "present": bool(bundles),
            "ready": len(bundles),
            "oldest_age_sec": max(0, now - oldest) if bundles else None,
            "egress": dict(egress),
            "tickets": tickets,
            "events": events,
            "last_publish": last,
        }

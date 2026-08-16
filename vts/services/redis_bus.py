from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Any

from redis.asyncio import Redis

from vts.core.config import Settings


# Only scan for stale throttle entries once the map is big enough that walking
# it is worth the cost; below this it cannot be leaking meaningfully anyway.
_THROTTLE_MAP_SCAN_THRESHOLD = 256
# How many throttle intervals an entry must be idle before it is dropped. One
# interval is enough to be functionally dead; the margin keeps a task that is
# merely between two slow progress ticks from being evicted and re-added.
_THROTTLE_STALE_INTERVALS = 20
# Hard ceiling, enforced after the age pass. Generous next to any realistic
# number of tasks publishing progress at once, so in practice only entries from
# finished tasks are ever dropped this way.
_THROTTLE_MAP_MAX_ENTRIES = 1024


class RedisBus:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.events_channel = f"{settings.redis_prefix}events"
        self.cancel_channel = f"{settings.redis_prefix}tasks:cancel"
        self._last_emit: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    def _cancel_key(self, task_id: uuid.UUID) -> str:
        return f"{self.settings.redis_prefix}task:{task_id}:cancel"

    def _pause_key(self, task_id: uuid.UUID) -> str:
        return f"{self.settings.redis_prefix}task:{task_id}:pause"

    async def notify_queued(self) -> None:
        """Wake the worker up via pub/sub after a task is committed to queued status."""
        await self.redis.publish(f"{self.settings.redis_prefix}queue:notify", "1")

    async def request_cancel(self, task_id: uuid.UUID) -> None:
        await self.redis.set(self._cancel_key(task_id), "1", ex=self.settings.task_cancel_ttl_seconds)
        await self.redis.publish(self.cancel_channel, str(task_id))

    async def clear_cancel_request(self, task_id: uuid.UUID) -> None:
        await self.redis.delete(self._cancel_key(task_id))

    async def is_cancel_requested(self, task_id: uuid.UUID) -> bool:
        return bool(await self.redis.exists(self._cancel_key(task_id)))

    async def request_pause(self, task_id: uuid.UUID) -> None:
        await self.redis.set(self._pause_key(task_id), "1", ex=self.settings.task_cancel_ttl_seconds)

    async def clear_pause_request(self, task_id: uuid.UUID) -> None:
        await self.redis.delete(self._pause_key(task_id))

    async def is_pause_requested(self, task_id: uuid.UUID) -> bool:
        return bool(await self.redis.exists(self._pause_key(task_id)))

    def _evict_stale_throttle_keys(self, now: float, interval: float) -> None:
        """Drop throttle entries that can no longer suppress anything (vts-swg1).

        The key is "{task_id}:{throttle_key}", so every task that ever emits a
        throttled event leaves an entry behind. RedisBus is a long-lived
        singleton in both the web app and the worker, so across thousands of
        tasks that map grows without bound — the same slow-leak shape as the
        FileHandler fd leak (vts-e5l).

        Eviction is by AGE rather than hooked to task completion on purpose: an
        entry older than one throttle interval can never suppress a publish
        again (the comparison against it always passes), so it is already dead
        whether or not its task has finished. That needs no cleanup call on
        every terminal path — a task that fails, is cancelled, is deleted
        mid-flight or dies with the worker would each have to remember, and the
        one that forgot would leak silently.

        Amortised: the scan runs only on a publish that was NOT throttled away,
        and only once the map is big enough to be worth walking, so the steady
        state for a handful of live tasks costs nothing.

        Age alone is not sufficient. A worker churning through short tasks can
        add thousands of entries faster than any of them ages out — measured,
        2000 tasks land in 34ms, far inside a 5s staleness window — so the map
        would still grow unbounded under exactly the workload that leaks. The
        age pass is therefore backed by a hard ceiling: once the map exceeds it,
        the oldest entries go regardless of age. Evicting a still-live task only
        costs one un-throttled publish, which is why the ceiling is set far
        above any plausible number of concurrent tasks.
        """
        if len(self._last_emit) < _THROTTLE_MAP_SCAN_THRESHOLD:
            return
        cutoff = now - max(interval, 0.0) * _THROTTLE_STALE_INTERVALS
        for key in [k for k, seen in self._last_emit.items() if seen < cutoff]:
            del self._last_emit[key]
        excess = len(self._last_emit) - _THROTTLE_MAP_MAX_ENTRIES
        if excess > 0:
            oldest = sorted(self._last_emit, key=self._last_emit.__getitem__)[:excess]
            for key in oldest:
                del self._last_emit[key]

    async def publish_event(
        self,
        *,
        user_id: str,
        task_id: str,
        event: str,
        data: dict[str, Any],
        throttle_key: str | None = None,
    ) -> None:
        if throttle_key:
            async with self._lock:
                now = time.monotonic()
                interval = 1.0 / max(self.settings.event_throttle_hz, 1)
                key = f"{task_id}:{throttle_key}"
                if now - self._last_emit[key] < interval:
                    return
                self._last_emit[key] = now
                self._evict_stale_throttle_keys(now, interval)
        payload = {
            "user_id": user_id,
            "task_id": task_id,
            "event": event,
            "data": data,
        }
        await self.redis.publish(self.events_channel, json.dumps(payload, ensure_ascii=True))

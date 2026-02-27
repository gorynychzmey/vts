from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Any

from redis.asyncio import Redis

from vts.core.config import Settings


class RedisBus:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.queue_key = f"{settings.redis_prefix}queue:tasks"
        self.queue_index_key = f"{settings.redis_prefix}queue:tasks:index"
        self.events_channel = f"{settings.redis_prefix}events"
        self._last_emit: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def enqueue_task(self, task_id: uuid.UUID) -> None:
        raw_task_id = str(task_id)
        added = await self.redis.sadd(self.queue_index_key, raw_task_id)
        if added:
            await self.redis.lpush(self.queue_key, raw_task_id)

    async def dequeue_task(self, timeout_seconds: int = 3) -> uuid.UUID | None:
        item = await self.redis.brpop(self.queue_key, timeout=timeout_seconds)
        if item is None:
            return None
        _, raw = item
        await self.redis.srem(self.queue_index_key, raw.decode("utf-8"))
        return uuid.UUID(raw.decode("utf-8"))

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
        payload = {
            "user_id": user_id,
            "task_id": task_id,
            "event": event,
            "data": data,
        }
        await self.redis.publish(self.events_channel, json.dumps(payload, ensure_ascii=True))

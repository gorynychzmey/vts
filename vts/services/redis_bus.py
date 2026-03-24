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
        self.events_channel = f"{settings.redis_prefix}events"
        self.cancel_channel = f"{settings.redis_prefix}tasks:cancel"
        self._last_emit: dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    def _cancel_key(self, task_id: uuid.UUID) -> str:
        return f"{self.settings.redis_prefix}task:{task_id}:cancel"

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

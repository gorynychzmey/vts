from __future__ import annotations

import asyncio
from datetime import datetime

from redis.asyncio import Redis

from vts.core.config import Settings


class HeavySlot:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.key = f"{settings.redis_prefix}heavy_slots"

    async def _wait_night_mode(self) -> None:
        if not self.settings.night_mode_enabled:
            return
        while True:
            now_hour = datetime.now().hour
            start = self.settings.night_mode_start_hour
            end = self.settings.night_mode_end_hour
            allowed = (start <= now_hour) or (now_hour < end) if start > end else start <= now_hour < end
            if allowed:
                return
            await asyncio.sleep(30)

    async def acquire(self) -> None:
        await self._wait_night_mode()
        limit = max(self.settings.heavy_slot_limit, 1)
        while True:
            current = await self.redis.incr(self.key)
            if current <= limit:
                return
            await self.redis.decr(self.key)
            await asyncio.sleep(1)

    async def release(self) -> None:
        value = await self.redis.decr(self.key)
        if value < 0:
            await self.redis.set(self.key, 0)

    async def __aenter__(self) -> "HeavySlot":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.release()


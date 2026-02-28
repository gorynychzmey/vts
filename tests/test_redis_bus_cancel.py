from __future__ import annotations

import uuid
from collections import defaultdict

from vts.core.config import Settings
from vts.services.redis_bus import RedisBus


class _FakeRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._sets: dict[str, set[str]] = defaultdict(set)
        self._lists: dict[str, list[str]] = defaultdict(list)
        self.published: list[tuple[str, str]] = []

    async def sadd(self, key: str, value: str) -> int:
        before = len(self._sets[key])
        self._sets[key].add(value)
        return 1 if len(self._sets[key]) > before else 0

    async def srem(self, key: str, value: str) -> int:
        if value in self._sets[key]:
            self._sets[key].remove(value)
            return 1
        return 0

    async def lpush(self, key: str, value: str) -> int:
        self._lists[key].insert(0, value)
        return len(self._lists[key])

    async def lrem(self, key: str, count: int, value: str) -> int:
        removed = 0
        if count == 0:
            original = list(self._lists[key])
            self._lists[key] = [item for item in original if item != value]
            removed = len(original) - len(self._lists[key])
        return removed

    async def brpop(self, key: str, timeout: int = 0) -> tuple[bytes, bytes] | None:
        values = self._lists.get(key, [])
        if not values:
            return None
        value = values.pop()
        return key.encode("utf-8"), value.encode("utf-8")

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._strings[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self._strings else 0

    async def delete(self, key: str) -> int:
        if key in self._strings:
            del self._strings[key]
            return 1
        return 0

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


def _settings() -> Settings:
    return Settings(redis_url="redis://fake:6379/0")


def test_request_cancel_and_clear_flag() -> None:
    fake = _FakeRedis()
    bus = RedisBus(fake, _settings())  # type: ignore[arg-type]
    task_id = uuid.uuid4()

    import asyncio

    asyncio.run(bus.request_cancel(task_id))
    assert asyncio.run(bus.is_cancel_requested(task_id)) is True
    assert fake.published and fake.published[-1][0] == bus.cancel_channel
    assert fake.published[-1][1] == str(task_id)

    asyncio.run(bus.clear_cancel_request(task_id))
    assert asyncio.run(bus.is_cancel_requested(task_id)) is False


def test_remove_task_from_queue_removes_index_and_list() -> None:
    fake = _FakeRedis()
    bus = RedisBus(fake, _settings())  # type: ignore[arg-type]
    task_id = uuid.uuid4()

    import asyncio

    asyncio.run(bus.enqueue_task(task_id))
    assert str(task_id) in fake._sets[bus.queue_index_key]
    assert str(task_id) in fake._lists[bus.queue_key]

    asyncio.run(bus.remove_task_from_queue(task_id))
    assert str(task_id) not in fake._sets[bus.queue_index_key]
    assert str(task_id) not in fake._lists[bus.queue_key]

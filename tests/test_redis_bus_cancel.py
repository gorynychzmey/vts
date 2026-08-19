from __future__ import annotations

import uuid
from vts.core.config import Settings
from vts.services.redis_bus import RedisBus


class _FakeRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

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




def test_request_restart_and_clear_flag() -> None:
    """Restarting a task the worker still holds is a deferred action.

    The endpoint cannot reset the artefacts itself while a worker is writing
    to them, so it marks the task and returns; the worker performs the reset
    once it has actually let go. That marker is the same ephemeral Redis flag
    the pause and cancel paths already use — including its TTL, so a marker
    orphaned by a crash expires instead of pinning the task forever.
    """
    import asyncio

    fake = _FakeRedis()
    bus = RedisBus(fake, _settings())  # type: ignore[arg-type]
    task_id = uuid.uuid4()

    assert asyncio.run(bus.is_restart_requested(task_id)) is False
    asyncio.run(bus.request_restart(task_id))
    assert asyncio.run(bus.is_restart_requested(task_id)) is True

    # Independent of the other two flags: clearing one must not clear it.
    asyncio.run(bus.clear_cancel_request(task_id))
    asyncio.run(bus.clear_pause_request(task_id))
    assert asyncio.run(bus.is_restart_requested(task_id)) is True

    asyncio.run(bus.clear_restart_request(task_id))
    assert asyncio.run(bus.is_restart_requested(task_id)) is False

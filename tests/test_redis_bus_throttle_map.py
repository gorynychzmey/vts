"""The publish throttle's bookkeeping must not grow without bound (vts-swg1).

RedisBus is a long-lived singleton in both the web app and the worker, and its
throttle map is keyed "{task_id}:{throttle_key}". Every task that ever emits a
throttled progress event therefore leaves an entry behind, and nothing used to
remove them — a slow leak across days of processing, the same shape as the
FileHandler fd leak (vts-e5l).

The interesting property is not "some cleanup function exists" but "the map
stays bounded while throttling still works", so both are asserted here.
"""

from __future__ import annotations

import asyncio
import uuid

from vts.core.config import Settings
from vts.services.redis_bus import RedisBus


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


def _bus(hz: int = 4) -> tuple[RedisBus, _FakeRedis]:
    fake = _FakeRedis()
    settings = Settings(redis_url="redis://fake:6379/0", event_throttle_hz=hz)
    return RedisBus(fake, settings), fake  # type: ignore[arg-type]


async def _emit(bus: RedisBus, task_id: str, key: str = "media_progress") -> None:
    await bus.publish_event(
        user_id="u", task_id=task_id, event="progress", data={}, throttle_key=key
    )


def test_throttle_map_does_not_grow_with_every_task() -> None:
    """Many short-lived tasks must not each leave a permanent entry.

    Simulates what a worker does over time: a long run of tasks that each emit
    a couple of throttled events and are then never seen again. Under the leak
    the map ends up with one entry per task; bounded, it settles far below.
    """
    bus, _fake = _bus()

    async def scenario() -> int:
        for _ in range(2000):
            task_id = str(uuid.uuid4())
            await _emit(bus, task_id)
            # A second emit for the same task, as a real task would.
            await _emit(bus, task_id)
        return len(bus._last_emit)

    size = asyncio.run(scenario())
    assert size < 2000, (
        f"throttle map holds {size} entries after 2000 finished tasks — "
        "one per task means nothing is ever evicted"
    )


def test_throttling_still_suppresses_rapid_events() -> None:
    """Eviction must not break what the map is for.

    A test that only checked the size would pass if the map were cleared on
    every call — which would disable throttling entirely and flood SSE.
    """
    bus, fake = _bus(hz=4)
    task_id = str(uuid.uuid4())

    async def scenario() -> None:
        for _ in range(50):
            await _emit(bus, task_id)

    asyncio.run(scenario())
    # 50 rapid emits inside one 250ms window: the first goes out, the rest are
    # suppressed. Allow a little slack in case the loop straddles a boundary.
    assert 1 <= len(fake.published) <= 3, (
        f"{len(fake.published)} of 50 rapid events were published — throttling is not working"
    )


def test_live_task_keeps_its_throttle_entry() -> None:
    """Eviction is by age, so a task still emitting must not lose its entry.

    If a live task's entry were dropped between two ticks, its next event would
    publish immediately and the throttle would be defeated exactly where it
    matters most — a busy task emitting fast.
    """
    bus, fake = _bus(hz=4)
    live = str(uuid.uuid4())

    async def scenario() -> None:
        await _emit(bus, live)
        # Push the map well past the scan threshold with other tasks.
        for _ in range(1000):
            await _emit(bus, str(uuid.uuid4()))
        # The live task emits again straight away; still inside its window.
        await _emit(bus, live)

    asyncio.run(scenario())
    published_for_live = [p for _c, p in fake.published if live in p]
    assert len(published_for_live) == 1, (
        f"the live task published {len(published_for_live)} times — its throttle entry "
        "was evicted while it was still active"
    )

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.models import Task, TaskStatus
from vts.db.repo import Repo
import vts.worker.main as main_mod
from vts.worker.main import WorkerPool


class FakeBus:
    """In-memory stand-in for RedisBus with the cancel and pause surface."""

    def __init__(self) -> None:
        self._cancels: set[uuid.UUID] = set()
        self._pauses: set[uuid.UUID] = set()
        self._restarts: set[uuid.UUID] = set()
        self.queued_notifications = 0

    async def request_cancel(self, task_id: uuid.UUID) -> None:
        self._cancels.add(task_id)

    async def clear_cancel_request(self, task_id: uuid.UUID) -> None:
        self._cancels.discard(task_id)

    async def is_cancel_requested(self, task_id: uuid.UUID) -> bool:
        return task_id in self._cancels

    async def request_pause(self, task_id: uuid.UUID) -> None:
        self._pauses.add(task_id)

    async def clear_pause_request(self, task_id: uuid.UUID) -> None:
        self._pauses.discard(task_id)

    async def is_pause_requested(self, task_id: uuid.UUID) -> bool:
        return task_id in self._pauses

    async def request_restart(self, task_id: uuid.UUID) -> None:
        self._restarts.add(task_id)

    async def clear_restart_request(self, task_id: uuid.UUID) -> None:
        self._restarts.discard(task_id)

    async def is_restart_requested(self, task_id: uuid.UUID) -> bool:
        return task_id in self._restarts

    async def notify_queued(self) -> None:
        self.queued_notifications += 1


class FakeProcessor:
    """Processor whose process_task blocks on a per-task Event so the test
    controls each task's lifecycle."""

    def __init__(self) -> None:
        self.entered: set[uuid.UUID] = set()
        self.cancelled: set[uuid.UUID] = set()
        self._release: dict[uuid.UUID, asyncio.Event] = {}

    def _event(self, task_id: uuid.UUID) -> asyncio.Event:
        ev = self._release.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._release[task_id] = ev
        return ev

    def release(self, task_id: uuid.UUID) -> None:
        self._event(task_id).set()

    async def process_task(self, task_id: uuid.UUID) -> None:
        self.entered.add(task_id)
        try:
            await self._event(task_id).wait()
        except asyncio.CancelledError:
            # Records that the pool forced the interruption, which is what the
            # grace-window tests need to tell apart from a task that stopped
            # itself.
            self.cancelled.add(task_id)
            raise


@pytest_asyncio.fixture
async def factory():
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_queued(factory, n: int) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with factory() as session:
        repo = Repo(session)
        user = await repo.get_or_create_user("pool@example.com")
        await session.flush()
        for i in range(n):
            task = Task(
                user_id=user.id,
                source_url=f"u{i}",
                status=TaskStatus.queued,
                options={},
                artifact_dir=f"/tmp/t{i}",
            )
            session.add(task)
            await session.flush()
            ids.append(task.id)
        await session.commit()
    return ids


async def _status(factory, task_id: uuid.UUID) -> TaskStatus:
    async with factory() as session:
        task = await session.get(Task, task_id)
        return task.status


@pytest.mark.asyncio
async def test_admit_claims_up_to_capacity(factory):
    await _seed_queued(factory, 3)
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=2)

    admitted = await pool.admit()

    assert admitted is True
    assert pool.active_count == 2
    # Third task stays queued.
    async with factory() as session:
        from sqlalchemy import func, select

        remaining = await session.scalar(
            select(func.count()).select_from(Task).where(Task.status == TaskStatus.queued)
        )
    assert remaining == 1

    # Clean up spawned coroutines.
    for tid in list(proc.entered):
        proc.release(tid)
    await pool.reap()


@pytest.mark.asyncio
async def test_two_admitted_run_concurrently(factory):
    ids = await _seed_queued(factory, 2)
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=2)

    await pool.admit()

    # Both coroutines must have entered before either was released.
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)
    assert proc.entered == set(ids)
    assert pool.active_count == 2

    for tid in ids:
        proc.release(tid)
    for _ in range(50):
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)
    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_watch_cancels_also_interrupts_a_paused_task(factory, monkeypatch):
    """A pause must free the GPU now, not at the next step boundary.

    Pausing was cooperative: `check_paused` is only consulted between windows,
    so a task summarizing a single large window ignored the request until the
    window finished. Measured on production 2026-08-19, that meant a task sat
    "paused" while the model kept generating for over an hour. The window in
    flight is forfeited — that is the accepted cost of stopping immediately.

    The interruption is now preceded by a grace window (see the two tests
    below); a step that ignores the pause flag still gets interrupted once it
    expires, which is what this asserts.
    """
    monkeypatch.setattr(main_mod, "_PAUSE_GRACE_S", 0.05)
    ids = await _seed_queued(factory, 1)
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=1)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    await bus.request_pause(ids[0])
    for _ in range(50):
        await pool.watch_cancels()
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)

    assert pool.active_count == 0, "a paused task must be interrupted, not left running"


@pytest.mark.asyncio
async def test_pause_grace_lets_a_step_stop_itself_without_a_forced_cancel(
    factory, monkeypatch
):
    """A pause must not be enforced before the step had a chance to react.

    Only the step can free the hardware: yt-dlp and the diarization sidecar are
    child processes, and `atask.cancel()` unwinds the awaiting coroutine
    without touching them. Both steps notice the pause flag from their progress
    callback and kill the child themselves — but download throttles that Redis
    lookup to one second and acts on the answer a tick later, so it needs a
    couple of seconds. `watch_cancels` used to cancel on the very tick that saw
    the flag, so the cancel essentially always won and the child survived the
    pause.

    Here the task finishes on its own inside the grace window; the pool must
    never have cancelled it.
    """
    monkeypatch.setattr(main_mod, "_PAUSE_GRACE_S", 1.0)
    ids = await _seed_queued(factory, 1)
    task_id = ids[0]
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=1)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    await bus.request_pause(task_id)
    # First tick only arms the grace window.
    await pool.watch_cancels()
    assert pool._active[task_id].cancelled() is False
    assert not pool._active[task_id].cancelling(), (
        "the pause must not be enforced on the tick that noticed it"
    )

    # The step reacts on its own, well inside the window.
    await asyncio.sleep(0.05)
    proc.release(task_id)
    for _ in range(50):
        await pool.watch_cancels()
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)

    assert pool.active_count == 0
    assert task_id not in pool._cancel_sent, (
        "a task that stopped itself inside the grace window must never be "
        "force-cancelled"
    )
    assert proc.cancelled == set(), "no forced cancellation should have reached the task"


@pytest.mark.asyncio
async def test_pause_grace_expires_and_the_task_is_cancelled(factory, monkeypatch):
    """The grace window is a grace, not an amnesty.

    A step that never looks at the pause flag (or is stuck somewhere with no
    progress callback at all) must still be interrupted once the window runs
    out — otherwise pausing such a task would silently do nothing.
    """
    monkeypatch.setattr(main_mod, "_PAUSE_GRACE_S", 0.2)
    ids = await _seed_queued(factory, 1)
    task_id = ids[0]
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=1)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    await bus.request_pause(task_id)
    await pool.watch_cancels()
    assert task_id not in pool._cancel_sent

    # Never released: the step ignores the pause the way a stuck step would.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await pool.watch_cancels()
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.02)

    assert pool.active_count == 0, (
        "a step that ignores the pause flag must be cancelled once the grace expires"
    )
    assert task_id in proc.cancelled


@pytest.mark.asyncio
async def test_cancel_is_never_graced(factory, monkeypatch):
    """A cancel discards the task, so there is nothing to wait for.

    The grace window exists so a step can shut its child process down cleanly
    and keep the work it already did. A cancel keeps neither, so gracing it
    would only delay freeing the slot.
    """
    monkeypatch.setattr(main_mod, "_PAUSE_GRACE_S", 30.0)
    ids = await _seed_queued(factory, 1)
    task_id = ids[0]
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=1)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    await bus.request_cancel(task_id)
    await pool.watch_cancels()

    assert task_id in pool._cancel_sent, "a cancel must fire on the tick that sees it"

    for _ in range(50):
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)
    assert pool.active_count == 0
    assert task_id in proc.cancelled


@pytest.mark.asyncio
async def test_watch_cancels_cancels_one_and_reap_drops_it(factory):
    ids = await _seed_queued(factory, 2)
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=2)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    victim = ids[0]
    survivor = ids[1]
    await bus.request_cancel(victim)
    await pool.watch_cancels()

    # Reap until the canceled task is collected.
    for _ in range(50):
        await pool.reap()
        if pool.active_count == 1:
            break
        await asyncio.sleep(0.01)

    assert pool.active_count == 1
    # Survivor still running.
    proc.release(survivor)
    for _ in range(50):
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)
    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_pre_start_cancel_skip(factory):
    ids = await _seed_queued(factory, 1)
    task_id = ids[0]
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=2)

    await bus.request_cancel(task_id)
    admitted = await pool.admit()

    assert admitted is False
    assert pool.active_count == 0
    assert task_id not in proc.entered
    assert await _status(factory, task_id) == TaskStatus.canceled
    # Cancel flag cleared.
    assert await bus.is_cancel_requested(task_id) is False


@pytest.mark.asyncio
async def test_pre_start_pause_skip(factory):
    """A queued task with a pending pause must not be started at all.

    Symmetric with the cancel skip above. Without it the task is admitted,
    begins a step, and the next watch_cancels tick interrupts it — and since
    nothing on that path clears the flag, the task is re-queued and the same
    cycle repeats on every restart until the flag's TTL runs out.
    """
    ids = await _seed_queued(factory, 1)
    task_id = ids[0]
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=2)

    await bus.request_pause(task_id)
    admitted = await pool.admit()

    assert admitted is False
    assert pool.active_count == 0
    assert task_id not in proc.entered, "the step must never start"
    assert await _status(factory, task_id) == TaskStatus.paused
    assert await bus.is_pause_requested(task_id) is False, (
        "the flag must be cleared, or the task loops on the next restart"
    )


@pytest.mark.asyncio
async def test_reap_performs_a_requested_restart(factory):
    """A restart asked for while the worker held the task is done on release.

    The endpoint cannot reset the artefacts under a running worker — it would
    race the very step it is trying to discard — so it flags the task and
    returns. The reset belongs here, at the one moment the task is provably
    nobody's: after `reap` has collected it.
    """
    ids = await _seed_queued(factory, 1)
    bus = FakeBus()
    proc = FakeProcessor()
    pool = WorkerPool(session_factory=factory, bus=bus, processor=proc, max_active=1)

    await pool.admit()
    for _ in range(50):
        if proc.entered == set(ids):
            break
        await asyncio.sleep(0.01)

    await bus.request_restart(ids[0])
    await bus.request_cancel(ids[0])
    await pool.watch_cancels()

    for _ in range(50):
        await pool.reap()
        if pool.active_count == 0:
            break
        await asyncio.sleep(0.01)

    assert pool.active_count == 0
    assert await bus.is_restart_requested(ids[0]) is False, "the flag must be cleared"

    async with factory() as session:
        row = await session.get(Task, ids[0])
        assert row is not None
        assert row.status == TaskStatus.queued, "a restarted task must be re-queued"
    assert bus.queued_notifications >= 1, "the worker must be woken for the re-queued task"

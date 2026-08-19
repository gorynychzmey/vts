"""The `except asyncio.CancelledError` path in TaskProcessor.process_task.

Pause now interrupts a running task the same way a cancel does — via
`atask.cancel()` from WorkerPool.watch_cancels — so this handler is the only
thing standing between an interrupted task and a row left `running` forever.
`running` is in neither RESUMABLE_STATUSES nor SKIPPABLE_ON_START_STATUSES, so
such a row is dead until the worker restarts.

These drive the REAL `process_task` against a real Postgres row, because the
risk lives in the interaction between the handler, the session and the ORM
object — a fake processor asserting `active_count == 0` cannot see any of it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.models import Task, TaskStatus
from vts.db.repo import Repo
from vts.pipeline.processor import TaskProcessor, _TaskGone


class _FakeBus:
    """Pause/cancel flags plus the event sink, in memory."""

    def __init__(self, paused: bool = False) -> None:
        self.events: list[dict] = []
        self._paused = paused
        self.cleared: list[uuid.UUID] = []

    async def is_pause_requested(self, task_id) -> bool:
        return self._paused

    async def clear_pause_request(self, task_id) -> None:
        self.cleared.append(task_id)
        self._paused = False

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)


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


def _processor(factory, bus: _FakeBus, *, refresh_raises: bool = False) -> TaskProcessor:
    """A real TaskProcessor with only the cancellation collaborators stubbed.

    Built with __new__ so the constructor's whisper/diarization/LLM backends
    are not created: the code under test is process_task's exception handling,
    which touches none of them. Everything that handler DOES call — the bus,
    the repo, the session, refresh_task — is real or a deliberate stub.
    """
    from vts.core.config import get_settings

    proc = TaskProcessor.__new__(TaskProcessor)
    proc.session_factory = factory
    proc.bus = bus
    proc.settings = get_settings()
    proc._task_metrics = {}
    proc._task_n_ctx = {}
    proc._interrupted = False

    class _Ctx:
        async def check_paused(self, task_id) -> None:
            # Deliberately a no-op. The cooperative pause check between steps
            # is the path this release stopped relying on; what is under test
            # is the FORCED interrupt that arrives mid-step, so the loop must
            # be allowed to enter the step and block there.
            return None

        async def refresh_task(self, session, task) -> None:
            # Only once the task is being torn down, so the row "disappears"
            # at the moment the handler needs it — the realistic ordering: the
            # user pauses, then deletes a second later. Raising on the step
            # loop's earlier calls instead would abort the task long before the
            # handler under test ever runs.
            if refresh_raises and proc._interrupted:
                # The documented contract: a row deleted mid-flight surfaces as
                # _TaskGone.
                raise _TaskGone()
            await session.refresh(task)

        def get_emitter(self, task_id):
            return None

    proc._ctx = _Ctx()
    return proc


async def _seed_running(factory, artifact_dir) -> uuid.UUID:
    async with factory() as session:
        repo = Repo(session)
        user = await repo.get_or_create_user("cancel@example.com")
        await session.flush()
        task = Task(
            user_id=user.id,
            source_url="u0",
            status=TaskStatus.running,
            options={},
            artifact_dir=str(artifact_dir),
        )
        session.add(task)
        await session.flush()
        task_id = task.id
        await session.commit()
    return task_id


async def _status(factory, task_id: uuid.UUID) -> TaskStatus:
    async with factory() as session:
        task = await session.get(Task, task_id)
        return task.status


async def _run_until_cancelled(proc: TaskProcessor, task_id: uuid.UUID, monkeypatch):
    """Run process_task with the step loop replaced by an interruptible sleep.

    Returns the asyncio.Task so the caller can assert on how it finished.
    """

    entered = asyncio.Event()

    async def _forever(*args, **kwargs):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(TaskProcessor, "_run_step", _forever)
    monkeypatch.setattr(TaskProcessor, "_try_donor_clone", _passthrough_clone)

    atask = asyncio.create_task(proc.process_task(task_id))
    # Wait for the real thing rather than spinning a fixed number of times:
    # process_task does several round trips to Postgres before the step loop,
    # so a bounded spin cancels it while it is still in setup — where the
    # handler under test never runs and the test passes for the wrong reason.
    await asyncio.wait_for(entered.wait(), timeout=10)
    return atask


async def _passthrough_clone(self, session, repo, task, task_id):
    return task


@pytest.mark.asyncio
async def test_pause_interrupt_records_paused_status(factory, monkeypatch, tmp_path):
    """A pause interrupt must land the task in `paused`, in the database.

    The previous test for this asserted only that the pool's active_count went
    back to zero, which a task left `running` satisfies just as well.
    """
    task_id = await _seed_running(factory, tmp_path)
    bus = _FakeBus(paused=True)
    proc = _processor(factory, bus)

    atask = await _run_until_cancelled(proc, task_id, monkeypatch)
    atask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await atask

    assert await _status(factory, task_id) == TaskStatus.paused
    assert bus.cleared == [task_id], "the pause flag must not outlive the pause"
    assert any(
        e.get("data", {}).get("status") == "paused" for e in bus.events
    ), "the UI is told about the pause"


@pytest.mark.asyncio
async def test_cancel_interrupt_stays_a_cancellation(factory, monkeypatch, tmp_path):
    """Without a pause flag the interrupt is a plain cancel.

    It must reach the caller as CancelledError — WorkerPool.reap distinguishes
    a cancelled task from a crashed one by exactly that — and must not write
    `paused` over the task.
    """
    task_id = await _seed_running(factory, tmp_path)
    bus = _FakeBus(paused=False)
    proc = _processor(factory, bus)

    atask = await _run_until_cancelled(proc, task_id, monkeypatch)
    atask.cancel()
    with pytest.raises(asyncio.CancelledError):
        await atask

    assert await _status(factory, task_id) != TaskStatus.paused


@pytest.mark.asyncio
async def test_failure_inside_the_handler_does_not_swallow_the_cancellation(
    factory, monkeypatch, tmp_path
):
    """C1: an exception in the handler must not REPLACE the CancelledError.

    The handler runs while a cancellation is already in flight and does five
    awaits before its re-raise. refresh_task is the realistic thrower: by
    contract it turns a mid-flight delete into _TaskGone, and the user who
    pauses a task is the one most likely to delete it seconds later.

    If _TaskGone escaped, it would reach WorkerPool.reap as an unhandled crash
    rather than a cancellation — and since every handler above has already run,
    nothing would set a status, leaving the row `running` forever.
    """
    task_id = await _seed_running(factory, tmp_path)
    bus = _FakeBus(paused=True)
    proc = _processor(factory, bus, refresh_raises=True)

    atask = await _run_until_cancelled(proc, task_id, monkeypatch)
    proc._interrupted = True
    atask.cancel()

    # The cancellation survives: not _TaskGone, not any other exception.
    with pytest.raises(asyncio.CancelledError):
        await atask

    assert atask.cancelled(), "reap must see a cancellation, not a crash"


@pytest.mark.asyncio
async def test_status_write_survives_a_second_cancel(factory, monkeypatch, tmp_path):
    """C2: a repeat cancel must not cost the task its status write.

    watch_cancels guards itself with `_cancel_sent`, but `cancel_all()` in the
    worker's teardown does not — so on every SIGTERM (i.e. every deploy) a task
    already cancelled for a pause gets cancelled again, landing on an await
    inside the handler. Without the shield the paused write is lost and the row
    stays `running`.
    """
    task_id = await _seed_running(factory, tmp_path)

    class _SlowBus(_FakeBus):
        async def is_pause_requested(self, task_id) -> bool:
            # Yield inside the handler, so the second cancel lands mid-flight
            # exactly the way the teardown race delivers it.
            await asyncio.sleep(0.05)
            return self._paused

    bus = _SlowBus(paused=True)
    proc = _processor(factory, bus)

    atask = await _run_until_cancelled(proc, task_id, monkeypatch)
    atask.cancel()
    # Let the handler enter and block on the await above, then cancel again.
    await asyncio.sleep(0)
    atask.cancel()

    with pytest.raises(asyncio.CancelledError):
        await atask

    assert await _status(factory, task_id) == TaskStatus.paused, (
        "the second cancel must not cost the task its status"
    )

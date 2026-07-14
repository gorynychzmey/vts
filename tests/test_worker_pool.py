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
from vts.worker.main import WorkerPool


class FakeBus:
    """In-memory stand-in for RedisBus with just the cancel surface."""

    def __init__(self) -> None:
        self._cancels: set[uuid.UUID] = set()

    async def request_cancel(self, task_id: uuid.UUID) -> None:
        self._cancels.add(task_id)

    async def clear_cancel_request(self, task_id: uuid.UUID) -> None:
        self._cancels.discard(task_id)

    async def is_cancel_requested(self, task_id: uuid.UUID) -> bool:
        return task_id in self._cancels


class FakeProcessor:
    """Processor whose process_task blocks on a per-task Event so the test
    controls each task's lifecycle."""

    def __init__(self) -> None:
        self.entered: set[uuid.UUID] = set()
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
        await self._event(task_id).wait()


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

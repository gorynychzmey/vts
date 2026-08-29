"""`awaiting_step` must not outlive the wait it describes (vts-47w6).

`set_awaiting_input` writes both the status and the step the task is waiting on.
Nothing used to clear the step again, so a task that resumed and finished kept
`awaiting_step="match_speakers"` forever: the API then served the contradictory
pair `status=completed, awaiting_step=match_speakers`.

The SPA never showed it, because every read pairs the step WITH the status
(`needsInput(status) && awaitingStep === "match_speakers"`). That is what kept
this invisible — and also why it is worth fixing rather than shrugging at: the
serialized task is a public contract (the MCP tools and any other client read
the same field), and a consumer that trusts `awaiting_step` alone is reading a
wait that ended long ago.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _awaiting_task(session) -> Task:
    repo = Repo(session)
    task = await repo.create_task(
        user_id=_USER,
        source_url="https://example.com/v",
        options={"diarize": True},
        artifact_dir="/tmp/x",
    )
    await repo.set_awaiting_input(task, "match_speakers")
    await session.commit()
    assert task.awaiting_step == "match_speakers"
    return task


@pytest.mark.asyncio
async def test_completing_clears_the_awaited_step(factory):
    async with factory() as session:
        task = await _awaiting_task(session)
        repo = Repo(session)
        await repo.set_task_status(task, TaskStatus.completed)
        await session.commit()
        assert task.status == TaskStatus.completed
        assert task.awaiting_step is None, (
            "a completed task still reports the step it was waiting on"
        )


@pytest.mark.asyncio
async def test_resuming_into_running_clears_the_awaited_step(factory):
    # The wait ends when the task starts moving again, not only when it
    # finishes: a running task that still names an awaited step is the same
    # contradiction one step earlier.
    async with factory() as session:
        task = await _awaiting_task(session)
        repo = Repo(session)
        await repo.set_task_status(task, TaskStatus.running)
        await session.commit()
        assert task.awaiting_step is None


@pytest.mark.asyncio
async def test_failing_clears_the_awaited_step(factory):
    async with factory() as session:
        task = await _awaiting_task(session)
        repo = Repo(session)
        await repo.set_task_status(task, TaskStatus.failed, "boom")
        await session.commit()
        assert task.awaiting_step is None
        # The failure reason is untouched by the cleanup.
        assert task.error_message == "boom"


@pytest.mark.asyncio
async def test_re_entering_awaiting_input_keeps_the_step(factory):
    # The cleanup must not fight set_awaiting_input: a task can legitimately go
    # back into a wait, and that wait has to survive.
    async with factory() as session:
        task = await _awaiting_task(session)
        repo = Repo(session)
        await repo.set_task_status(task, TaskStatus.running)
        await session.commit()
        await repo.set_awaiting_input(task, "match_speakers")
        await session.commit()
        assert task.status == TaskStatus.awaiting_input
        assert task.awaiting_step == "match_speakers"


@pytest.mark.asyncio
async def test_serialized_task_never_pairs_completed_with_an_awaited_step(factory):
    # The contract as a client sees it, not just the column: this is the pair
    # that was observed on task 39350783 in production.
    from vts.api._helpers.serialization import serialize_task

    async with factory() as session:
        task = await _awaiting_task(session)
        repo = Repo(session)
        await repo.set_task_status(task, TaskStatus.completed)
        await session.commit()
        # Re-read through the repo: serialize_task walks task.steps, which the
        # in-memory row from create_task has not loaded.
        stored = await repo.get_task_by_id(task.id)
        payload = serialize_task(stored)
        assert payload.status == TaskStatus.completed.value
        assert payload.awaiting_step is None

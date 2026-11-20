"""Recording.state: completeness of the library entry, independent of the task (vts-lzad)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, RecordingState, Task, TaskStatus, User
from vts.services.recording_state import (
    backfill_recording_states,
    initial_state,
    state_for_status,
)

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


@pytest.mark.parametrize("status,expected", [
    (TaskStatus.completed, RecordingState.complete),
    (TaskStatus.failed, RecordingState.incomplete),
    (TaskStatus.canceled, RecordingState.incomplete),
    (TaskStatus.queued, RecordingState.processing),
    (TaskStatus.running, RecordingState.processing),
    (TaskStatus.paused, None), (TaskStatus.waiting, None),
    (TaskStatus.awaiting_input, None), (TaskStatus.archived, None),
])
def test_state_for_status(status, expected):
    assert state_for_status(status) is expected


def test_initial_state_archived_with_summary_is_complete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path="/s", prompts_requested=True) is RecordingState.complete


def test_initial_state_archived_without_summary_but_prompts_is_incomplete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path=None, prompts_requested=True) is RecordingState.incomplete


def test_initial_state_archived_transcript_only_task_is_complete():
    assert initial_state(TaskStatus.archived, transcript_path="/t", summary_path=None, prompts_requested=False) is RecordingState.complete


def test_initial_state_archived_nothing_is_incomplete():
    assert initial_state(TaskStatus.archived, transcript_path=None, summary_path=None, prompts_requested=False) is RecordingState.incomplete


@pytest.mark.parametrize("status", [TaskStatus.paused, TaskStatus.waiting, TaskStatus.awaiting_input])
def test_initial_state_in_flight_statuses_are_processing(status):
    assert initial_state(status, transcript_path=None, summary_path=None, prompts_requested=False) is RecordingState.processing


async def test_backfill_sets_state_from_task_and_detached(factory):
    async def add(session, name, status, *, transcript, summary=None, prompts=None, detached=False):
        task = Task(
            user_id=_USER, source_url=f"https://example.com/{name}", status=status,
            options={"prompts": prompts} if prompts is not None else {},
            artifact_dir=f"/tmp/{name}",
        )
        session.add(task)
        await session.flush()
        session.add(Recording(
            user_id=_USER, source_task_id=None if detached else task.id, title=name,
            artifact_dir=f"/tmp/{name}",
            transcript_path="/t" if transcript else None, summary_path=summary,
        ))

    async with factory() as s:
        await add(s, "completed", TaskStatus.completed, transcript=True, summary="/s", prompts=["p"])
        await add(s, "failed", TaskStatus.failed, transcript=True)
        await add(s, "archived", TaskStatus.archived, transcript=True, prompts=["p"])
        await add(s, "archived_text", TaskStatus.archived, transcript=True)
        await add(s, "detached_text", TaskStatus.completed, transcript=True, detached=True)
        await add(s, "detached_empty", TaskStatus.completed, transcript=False, detached=True)
        await s.commit()
        # Start from a uniform wrong value so the backfill has to do the work.
        async with s.bind.begin() as conn:
            await conn.exec_driver_sql("UPDATE recordings SET state = 'processing'")
            n = await conn.run_sync(backfill_recording_states)

    async with factory() as s:
        rows = (await s.execute(select(Recording))).scalars().all()
        states = {r.title: r.state.value for r in rows}
    assert n == 6
    assert states == {
        "completed": "complete", "failed": "incomplete", "archived": "incomplete",
        "archived_text": "complete", "detached_text": "complete",
        "detached_empty": "incomplete",
    }

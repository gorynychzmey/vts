"""Recording.state: completeness of the library entry, independent of the task (vts-lzad)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, RecordingState, Task, TaskStatus, User
from vts.services.recording_backfill import backfill_recording_states
from vts.services.recording_state import initial_state, state_for_status

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


_SYS = "system:summary"


def _done(*refs, status="completed"):
    out = []
    for r in refs:
        source, _, rid = r.partition(":")
        out.append({"source": source, "id": rid, "name": rid, "path": f"/p/{rid}", "status": status})
    return out


def _archived(options, *, transcript="/t", summary=None):
    # The prompt-result index belongs to the recording; the cases below write
    # it next to what the task requested for readability only.
    options = dict(options)
    results = options.pop("prompt_results", None)
    return initial_state(
        TaskStatus.archived, transcript_path=transcript, summary_path=summary,
        prompt_results=results, options=options,
    )


def test_initial_state_archived_with_summary_is_complete():
    assert _archived({"prompts": [_SYS]}, summary="/s") is RecordingState.complete


def test_initial_state_archived_summary_requested_but_missing_is_incomplete():
    assert _archived({"prompts": [_SYS]}) is RecordingState.incomplete


def test_initial_state_archived_transcript_only_task_is_complete():
    assert _archived({"prompts": []}) is RecordingState.complete


def test_initial_state_archived_nothing_is_incomplete():
    assert _archived({"prompts": []}, transcript=None) is RecordingState.incomplete


def test_initial_state_archived_custom_prompt_only_with_result_is_complete():
    options = {"prompts": ["user:memo"], "prompt_results": _done("user:memo")}
    assert _archived(options, summary=None) is RecordingState.complete


def test_initial_state_archived_custom_prompt_result_not_completed_is_incomplete():
    options = {"prompts": ["user:memo"], "prompt_results": _done("user:memo", status="pending")}
    assert _archived(options) is RecordingState.incomplete


def test_initial_state_archived_summary_plus_custom_missing_is_incomplete():
    options = {"prompts": [_SYS, "user:memo"], "prompt_results": []}
    assert _archived(options, summary="/s") is RecordingState.incomplete


def test_initial_state_archived_summary_plus_custom_all_done_is_complete():
    options = {"prompts": [_SYS, "user:memo"], "prompt_results": _done("user:memo")}
    assert _archived(options, summary="/s") is RecordingState.complete


def test_initial_state_archived_legacy_options_without_summary_is_incomplete():
    assert _archived({}, summary=None) is RecordingState.incomplete


def test_initial_state_archived_legacy_options_with_summary_is_complete():
    assert _archived({}, summary="/s") is RecordingState.complete


def test_initial_state_archived_legacy_summary_false_is_transcript_only():
    assert _archived({"summary": False}) is RecordingState.complete


@pytest.mark.parametrize("status", [TaskStatus.paused, TaskStatus.waiting, TaskStatus.awaiting_input])
def test_initial_state_in_flight_statuses_are_processing(status):
    assert initial_state(status, transcript_path=None, summary_path=None, prompt_results=[], options={}) is RecordingState.processing


async def test_backfill_sets_state_from_task_and_detached(factory):
    async def add(session, name, status, *, transcript, summary=None, prompts=None, results=None, detached=False):
        task = Task(
            user_id=_USER, source_url=f"https://example.com/{name}", status=status,
            options=(
                {"prompts": prompts, **({"prompt_results": results} if results else {})}
                if prompts is not None else {}
            ),
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
        await add(s, "completed", TaskStatus.completed, transcript=True, summary="/s", prompts=[_SYS])
        await add(s, "failed", TaskStatus.failed, transcript=True)
        await add(s, "archived", TaskStatus.archived, transcript=True, prompts=[_SYS])
        await add(
            s, "archived_memo", TaskStatus.archived, transcript=True,
            prompts=["user:memo"], results=_done("user:memo"),
        )
        await add(s, "archived_text", TaskStatus.archived, transcript=True, prompts=[])
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
    assert n == 7
    assert states == {
        "completed": "complete", "failed": "incomplete", "archived": "incomplete",
        "archived_memo": "complete", "archived_text": "complete", "detached_text": "complete",
        "detached_empty": "incomplete",
    }


async def _task_with_recording(factory, status=TaskStatus.running, *, recording=True):
    """A task (and optionally its recording) committed; returns the task id."""
    async with factory() as s:
        task = Task(
            user_id=_USER, source_url="https://example.com/x", status=status,
            options={}, artifact_dir="/tmp/x",
        )
        s.add(task)
        await s.flush()
        if recording:
            s.add(Recording(
                user_id=_USER, source_task_id=task.id, title="x", artifact_dir="/tmp/x",
                state=RecordingState.processing,
            ))
        await s.commit()
        return task.id


async def _state(factory, task_id):
    async with factory() as s:
        return await s.scalar(select(Recording.state).where(Recording.source_task_id == task_id))


async def _set_status(factory, task_id, status):
    from vts.db.repo import Repo
    async with factory() as s:
        task = await s.get(Task, task_id)
        await Repo(s).set_task_status(task, status)
        await s.commit()


async def test_completing_a_task_completes_its_recording(factory):
    tid = await _task_with_recording(factory)
    await _set_status(factory, tid, TaskStatus.completed)
    assert await _state(factory, tid) is RecordingState.complete


async def test_failed_then_archived_stays_incomplete(factory):
    tid = await _task_with_recording(factory)
    await _set_status(factory, tid, TaskStatus.failed)
    await _set_status(factory, tid, TaskStatus.archived)
    assert await _state(factory, tid) is RecordingState.incomplete


async def test_archived_from_awaiting_input_closes_processing_recording(factory):
    tid = await _task_with_recording(factory, TaskStatus.awaiting_input)
    await _set_status(factory, tid, TaskStatus.archived)
    assert await _state(factory, tid) is RecordingState.incomplete


async def test_completed_then_archived_stays_complete(factory):
    tid = await _task_with_recording(factory)
    await _set_status(factory, tid, TaskStatus.completed)
    await _set_status(factory, tid, TaskStatus.archived)
    assert await _state(factory, tid) is RecordingState.complete


async def test_archiving_by_id_closes_processing_recording(factory):
    from vts.db.repo import Repo
    tid = await _task_with_recording(factory, TaskStatus.awaiting_input)
    async with factory() as s:
        await Repo(s).set_task_status_by_id(tid, TaskStatus.archived)
        await s.commit()
    assert await _state(factory, tid) is RecordingState.incomplete


async def test_restart_puts_complete_recording_back_to_processing(factory):
    tid = await _task_with_recording(factory)
    await _set_status(factory, tid, TaskStatus.completed)
    assert await _state(factory, tid) is RecordingState.complete
    await _set_status(factory, tid, TaskStatus.queued)
    assert await _state(factory, tid) is RecordingState.processing


async def test_cancel_by_id_marks_incomplete(factory):
    from vts.db.repo import Repo
    tid = await _task_with_recording(factory)
    async with factory() as s:
        await Repo(s).set_task_status_by_id(tid, TaskStatus.canceled)
        await s.commit()
    assert await _state(factory, tid) is RecordingState.incomplete


async def test_status_change_without_recording_is_a_noop(factory):
    from vts.db.repo import Repo
    tid = await _task_with_recording(factory, recording=False)
    await _set_status(factory, tid, TaskStatus.completed)
    async with factory() as s:
        await Repo(s).set_task_status_by_id(tid, TaskStatus.failed)
        await s.commit()
    assert await _state(factory, tid) is None


async def test_upsert_creating_for_completed_task_starts_complete(factory):
    from vts.db.repo import Repo
    tid = await _task_with_recording(factory, TaskStatus.completed, recording=False)
    async with factory() as s:
        task = await s.get(Task, tid)
        rec = await Repo(s).upsert_recording_for_task(task)
        await s.commit()
        assert rec.state is RecordingState.complete


async def test_upsert_updating_keeps_existing_state(factory):
    from vts.db.repo import Repo
    tid = await _task_with_recording(factory, TaskStatus.completed)
    async with factory() as s:
        task = await s.get(Task, tid)
        rec = await Repo(s).upsert_recording_for_task(task)
        await s.commit()
        assert rec.state is RecordingState.processing

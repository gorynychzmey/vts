"""vts-d64: a task deleted mid-flight (delete/cancel) must NOT be reported as
`failed`.

The API delete endpoint sets the row to `canceled` and then physically deletes
it in its own session. The worker coroutine is meanwhile looping over steps and
calls `session.refresh(task)` between iterations. On a deleted row SQLAlchemy's
`refresh` raises `sqlalchemy.exc.InvalidRequestError` ("Could not refresh
instance"). That exception used to be caught by `process_task`'s broad
`except Exception`, which published a spurious `task_status=failed` event and a
failure push before the pool could cancel the coroutine.

These tests drive the REAL `process_task` against a real in-memory SQLite
session (so `refresh` genuinely raises on the deleted row) and assert the task
returns quietly, emitting no `failed` event.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.pipeline.context import PipelineContext
from vts.pipeline.processor import TaskProcessor


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_event(self, **kwargs) -> None:
        self.events.append(kwargs)

    async def clear_pause_request(self, task_id) -> None:
        return None

    async def is_pause_requested(self, task_id) -> bool:
        return False


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        features_donor_clone=False,
        metrics_enabled=False,
        metrics_jsonl_path=None,
        media_ttl_hours=0,
        services_database_write_throttle_ms=0,
        timezone=None,
    )


async def _make_engine_and_task(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    tid = uuid.uuid4()
    artifact = tmp_path / "task"
    (artifact / "logs").mkdir(parents=True, exist_ok=True)
    async with session_factory() as session:
        session.add(User(id=uid, username="u"))
        session.add(
            Task(
                id=tid,
                user_id=uid,
                source_url="http://x/v",
                status=TaskStatus.queued,
                options={},
                artifact_dir=str(artifact),
            )
        )
        await session.commit()
    return engine, session_factory, tid


def _make_processor(session_factory, bus, tmp_path) -> TaskProcessor:
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.session_factory = session_factory
    proc.bus = bus
    proc.settings = _settings(tmp_path)
    proc._task_metrics = {}
    proc._task_n_ctx = {}
    # process_task drives its infra through the PipelineContext; give the bare
    # processor a context bound to the same session_factory/bus/settings so
    # check_paused / refresh_task / send_push_safe resolve.
    ctx = PipelineContext.__new__(PipelineContext)
    ctx.session_factory = session_factory
    ctx.bus = bus
    ctx.settings = proc.settings
    proc._ctx = ctx
    return proc


@pytest.mark.asyncio
async def test_task_deleted_midflight_does_not_emit_failed(tmp_path, monkeypatch) -> None:
    engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus, tmp_path)

    # One dummy step; delete the row while the first step "runs" so the next
    # session.refresh(task) hits the deleted row.
    monkeypatch.setattr(
        "vts.pipeline.processor.build_dag_steps", lambda opts: ["download"]
    )

    async def _fake_run_step(self, session, repo, task_id, user_id, step_name, dirs, logger, options):
        # Simulate the API deleting the row in a separate session mid-step.
        async with session_factory() as other:
            row = (await other.execute(select(Task).where(Task.id == tid))).scalar_one()
            await other.delete(row)
            await other.commit()

    monkeypatch.setattr(TaskProcessor, "_run_step", _fake_run_step)

    async def _noop_push(session, user_id, payload) -> None:
        _noop_push.calls.append(payload)

    _noop_push.calls = []
    monkeypatch.setattr(proc._ctx, "send_push_safe", _noop_push)

    await proc.process_task(tid)

    failed_events = [
        e for e in bus.events
        if e.get("event") == "task_status" and e.get("data", {}).get("status") == "failed"
    ]
    assert failed_events == [], f"spurious failed event(s): {failed_events}"
    failed_pushes = [p for p in _noop_push.calls if p.get("status") == "failed"]
    assert failed_pushes == [], f"spurious failed push(es): {failed_pushes}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_real_step_failure_still_emits_failed(tmp_path, monkeypatch) -> None:
    """Guard: the _TaskGone quiet-exit must NOT swallow genuine pipeline
    errors — a real step exception must still publish `failed`."""
    engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus, tmp_path)

    monkeypatch.setattr(
        "vts.pipeline.processor.build_dag_steps", lambda opts: ["download"]
    )

    async def _boom_run_step(self, session, repo, task_id, user_id, step_name, dirs, logger, options):
        raise RuntimeError("boom")

    monkeypatch.setattr(TaskProcessor, "_run_step", _boom_run_step)

    async def _noop_push(session, user_id, payload) -> None:
        return None

    monkeypatch.setattr(proc._ctx, "send_push_safe", _noop_push)

    await proc.process_task(tid)

    failed_events = [
        e for e in bus.events
        if e.get("event") == "task_status" and e.get("data", {}).get("status") == "failed"
    ]
    assert len(failed_events) == 1, f"expected exactly one failed event, got {failed_events}"
    assert failed_events[0]["data"]["error"] == "boom"

    await engine.dispose()

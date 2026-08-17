"""The donor-clone fast path in `process_task`.

When another user already transcribed the same URL with the same options, the
task is satisfied by copying that result instead of re-running the pipeline.
The path had no tests, which mattered because two of its three outcomes are
error handling: a failed lookup and a failed clone must both fall back to the
normal pipeline rather than failing the task.

These drive the real `process_task` against a real Postgres session, as
test_process_task_deleted_midflight does.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from _db import make_test_engine
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


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        features_donor_clone=True,
        metrics_enabled=False,
        metrics_jsonl_path=None,
        media_ttl_hours=0,
        services_database_write_throttle_ms=0,
        timezone=None,
    )


async def _make_engine_and_task(tmp_path: Path):
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
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


def _make_processor(session_factory, bus) -> TaskProcessor:
    proc = TaskProcessor.__new__(TaskProcessor)
    proc.session_factory = session_factory
    proc.bus = bus
    proc.settings = _settings()
    proc._task_metrics = {}
    proc._task_n_ctx = {}
    ctx = PipelineContext.__new__(PipelineContext)
    ctx.session_factory = session_factory
    ctx.bus = bus
    ctx.settings = proc.settings
    proc._ctx = ctx
    return proc


def _statuses(bus: _CapturingBus) -> list[str]:
    return [
        e.get("data", {}).get("status")
        for e in bus.events
        if e.get("event") == "task_status"
    ]


@pytest.mark.asyncio
async def test_successful_clone_completes_without_running_any_step(
    tmp_path, monkeypatch
) -> None:
    """The whole point of the fast path: no pipeline step may run."""
    _engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus)

    donor = SimpleNamespace(id=uuid.uuid4())

    async def _find_donor(self, **kwargs):
        return donor

    monkeypatch.setattr("vts.db.repo.Repo.find_completed_donor", _find_donor)

    cloned: list = []

    async def _clone(self, session, repo, task, found_donor):
        cloned.append(found_donor)

    monkeypatch.setattr(TaskProcessor, "_clone_from_donor", _clone)

    ran: list = []

    async def _run_step(self, *a, **kw):
        ran.append(a)

    monkeypatch.setattr(TaskProcessor, "_run_step", _run_step)

    await proc.process_task(tid)

    assert cloned == [donor]
    assert ran == [], "the donor path must short-circuit before any step runs"
    assert _statuses(bus) == [TaskStatus.completed.value]


@pytest.mark.asyncio
async def test_donor_lookup_failure_falls_back_to_the_pipeline(
    tmp_path, monkeypatch
) -> None:
    """A broken lookup must not fail the task — it is an optimisation."""
    _engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus)

    async def _boom(self, **kwargs):
        raise RuntimeError("donor lookup exploded")

    monkeypatch.setattr("vts.db.repo.Repo.find_completed_donor", _boom)
    monkeypatch.setattr(
        "vts.pipeline.processor.build_dag_steps", lambda opts: ["download"]
    )

    ran: list = []

    async def _run_step(self, session, repo, task_id, user_id, step_name, *a, **kw):
        ran.append(step_name)

    monkeypatch.setattr(TaskProcessor, "_run_step", _run_step)

    async def _noop_push(session, user_id, payload) -> None:
        return None

    monkeypatch.setattr(proc._ctx, "send_push_safe", _noop_push)

    await proc.process_task(tid)

    assert ran == ["download"], "pipeline must still run when the lookup fails"
    assert TaskStatus.failed.value not in _statuses(bus)
    assert _statuses(bus)[-1] == TaskStatus.completed.value


@pytest.mark.asyncio
async def test_clone_failure_rolls_back_and_falls_back_to_the_pipeline(
    tmp_path, monkeypatch
) -> None:
    """A half-applied clone must be rolled back, then the task runs normally.

    This is the subtle one: the clone writes through the same session, so a
    failure mid-way leaves it dirty. Without the rollback the fallback would
    run on top of a partially cloned task.
    """
    _engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus)

    donor = SimpleNamespace(id=uuid.uuid4())

    async def _find_donor(self, **kwargs):
        return donor

    monkeypatch.setattr("vts.db.repo.Repo.find_completed_donor", _find_donor)

    async def _clone_boom(self, session, repo, task, found_donor):
        # Dirty the session the way a real half-applied clone would, so the
        # rollback has something to undo. Without it this test passes even if
        # the rollback is deleted.
        task.source_title = "HALF-CLONED"
        raise RuntimeError("clone exploded halfway")

    monkeypatch.setattr(TaskProcessor, "_clone_from_donor", _clone_boom)
    monkeypatch.setattr(
        "vts.pipeline.processor.build_dag_steps", lambda opts: ["download"]
    )

    ran: list = []

    async def _run_step(self, session, repo, task_id, user_id, step_name, *a, **kw):
        ran.append(step_name)

    monkeypatch.setattr(TaskProcessor, "_run_step", _run_step)

    async def _noop_push(session, user_id, payload) -> None:
        return None

    monkeypatch.setattr(proc._ctx, "send_push_safe", _noop_push)

    await proc.process_task(tid)

    assert ran == ["download"], "pipeline must run after a failed clone"
    assert TaskStatus.failed.value not in _statuses(bus)
    assert _statuses(bus)[-1] == TaskStatus.completed.value

    # The half-applied write must not have survived: the fallback pipeline has
    # to start from the task as it was, not from a partially cloned one.
    async with session_factory() as check:
        row = await check.get(Task, tid)
        assert row is not None
        assert row.source_title != "HALF-CLONED", (
            "partial clone leaked into the task — the rollback did not happen"
        )


@pytest.mark.asyncio
async def test_feature_flag_off_skips_the_lookup_entirely(
    tmp_path, monkeypatch
) -> None:
    """With the flag off the donor query must not even be issued."""
    _engine, session_factory, tid = await _make_engine_and_task(tmp_path)
    bus = _CapturingBus()
    proc = _make_processor(session_factory, bus)
    proc.settings.features_donor_clone = False
    proc._ctx.settings = proc.settings

    looked_up: list = []

    async def _find_donor(self, **kwargs):
        looked_up.append(kwargs)
        return None

    monkeypatch.setattr("vts.db.repo.Repo.find_completed_donor", _find_donor)
    monkeypatch.setattr(
        "vts.pipeline.processor.build_dag_steps", lambda opts: ["download"]
    )

    async def _run_step(self, *a, **kw):
        return None

    monkeypatch.setattr(TaskProcessor, "_run_step", _run_step)

    async def _noop_push(session, user_id, payload) -> None:
        return None

    monkeypatch.setattr(proc._ctx, "send_push_safe", _noop_push)

    await proc.process_task(tid)

    assert looked_up == [], "donor lookup ran despite features_donor_clone=False"

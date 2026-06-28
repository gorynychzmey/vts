from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.repo import Repo
from vts.db.models import Task, Step, TaskStatus, StepStatus
from vts.metrics.step_weights import StepDuration

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as sess:
            yield sess
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _make_completed_task(repo, user_id, total_windows, step_specs):
    # step_specs: list[(name, started, finished, status)]
    task = Task(
        user_id=user_id, source_url="u", status=TaskStatus.completed,
        options={}, artifact_dir="/tmp/x", summary_progress={"current": total_windows, "total": total_windows},
    )
    repo.session.add(task)
    await repo.session.flush()
    for name, started, finished, status in step_specs:
        repo.session.add(Step(task_id=task.id, name=name, status=status,
                              started_at=started, finished_at=finished))
    await repo.session.flush()
    return task


async def test_step_durations_for_user_only_completed(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("durations@example.com")
    await session.flush()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta
    await _make_completed_task(repo, user.id, 6, [
        ("download", t0, t0 + timedelta(seconds=10), StepStatus.completed),
        ("summarize_windows", t0, t0 + timedelta(seconds=60), StepStatus.completed),
        ("merge_transcript", t0, t0 + timedelta(seconds=5), StepStatus.failed),  # excluded
    ])
    await session.commit()
    rows = await repo.step_durations_for_user(user.id)
    names = sorted(r.name for r in rows)
    assert names == ["download", "summarize_windows"]
    sw = next(r for r in rows if r.name == "summarize_windows")
    assert sw.window_total == 6
    assert abs(sw.duration_sec - 60.0) < 0.01


async def test_upsert_and_get_user_step_weights(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("upsert@example.com")
    await session.flush()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    await repo.upsert_user_step_weights(user.id, {"download": 9.0}, 500.0, now, {"download": 7})
    await session.commit()
    row = await repo.get_user_step_weights(user.id)
    assert row.weights == {"download": 9.0}
    assert row.final_summary_fallback == 500.0
    # upsert again -> single row, updated
    await repo.upsert_user_step_weights(user.id, {"download": 1.0}, 1.0, now, {"download": 1})
    await session.commit()
    row2 = await repo.get_user_step_weights(user.id)
    assert row2.weights == {"download": 1.0}


async def test_users_with_completed_tasks(session):
    repo = Repo(session)
    u1 = await repo.get_or_create_user("has-completed@example.com")
    u2 = await repo.get_or_create_user("no-completed@example.com")
    await session.flush()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await _make_completed_task(repo, u1.id, 3, [])
    session.add(Task(user_id=u2.id, source_url="u", status=TaskStatus.queued,
                        options={}, artifact_dir="/tmp/y"))
    await session.commit()
    ids = await repo.users_with_completed_tasks()
    assert u1.id in ids
    assert u2.id not in ids

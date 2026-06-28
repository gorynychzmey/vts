import uuid
from datetime import datetime, timezone, timedelta
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.repo import Repo
from vts.db.models import Task, Step, TaskStatus, StepStatus
from vts.services.step_weights_recompute import recompute_for_user
from vts.metrics.step_weights import SEED_STEP_WEIGHTS

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


async def _completed_task(session, user_id, total, steps):
    t = Task(user_id=user_id, source_url="u", status=TaskStatus.completed,
             options={}, artifact_dir="/tmp/x",
             summary_progress={"current": total, "total": total})
    session.add(t)
    await session.flush()
    for name, dur in steps:
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.add(Step(task_id=t.id, name=name, status=StepStatus.completed,
                         started_at=t0, finished_at=t0 + timedelta(seconds=dur)))
    await session.flush()


async def test_recompute_below_threshold_keeps_seed(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("recompute1@example.com")
    await session.flush()
    # Only 2 download samples (< default 5) -> seed kept for download
    await _completed_task(session, user.id, 6, [("download", 99.0)])
    await _completed_task(session, user.id, 6, [("download", 99.0)])
    await session.commit()
    wrote = await recompute_for_user(session, user.id, min_samples=5)
    await session.commit()
    assert wrote is True
    row = await repo.get_user_step_weights(user.id)
    assert row.weights["download"] == SEED_STEP_WEIGHTS["download"]  # seed, not 99.0


async def test_recompute_above_threshold_uses_computed(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("recompute2@example.com")
    await session.flush()
    for _ in range(5):
        await _completed_task(session, user.id, 6, [("download", 42.0)])
    await session.commit()
    await recompute_for_user(session, user.id, min_samples=5)
    await session.commit()
    row = await repo.get_user_step_weights(user.id)
    assert row.weights["download"] == 42.0
    assert row.sample_counts["download"] == 5


async def test_recompute_no_data_returns_false(session):
    repo = Repo(session)
    user = await repo.get_or_create_user("recompute3@example.com")
    await session.flush()
    await session.commit()
    wrote = await recompute_for_user(session, user.id, min_samples=5)
    assert wrote is False
    assert await repo.get_user_step_weights(user.id) is None

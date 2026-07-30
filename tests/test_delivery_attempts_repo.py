from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import DeliveryStatus, Task, TaskStatus, User
from vts.db.repo import Repo

from _db import make_test_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Postgres-backed async session for delivery-attempt repo integration tests.

    Drops+recreates the schema around each test so tests don't bleed state.
    """
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _task(session: AsyncSession) -> Task:
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    t = Task(user_id=u.id, source_url="http://x", options={},
              artifact_dir="/tmp/x", status=TaskStatus.completed)
    session.add(t)
    await session.flush()
    return t


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_flips_to_delivering(session):
    repo = Repo(session)
    t = await _task(session)
    now = datetime.now(timezone.utc)
    await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="summary",
        max_attempts=3, next_attempt_at=now - timedelta(seconds=1))
    await session.commit()
    claimed = await repo.claim_due_deliveries(now, limit=10)
    assert len(claimed) == 1
    assert claimed[0].status == DeliveryStatus.delivering
    assert claimed[0].attempts == 1


@pytest.mark.asyncio
async def test_record_success(session):
    repo = Repo(session)
    t = await _task(session)
    now = datetime.now(timezone.utc)
    a = await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="raw",
        max_attempts=3, next_attempt_at=now)
    await session.commit()
    await repo.record_delivery_result(a.id, external_id="doc1", external_url="http://o/doc1")
    await session.commit()
    rows = await repo.list_deliveries_for_task(t.id)
    assert rows[0].status == DeliveryStatus.delivered
    assert rows[0].external_url == "http://o/doc1"


@pytest.mark.asyncio
async def test_failure_dead_vs_retry(session):
    repo = Repo(session)
    t = await _task(session)
    now = datetime.now(timezone.utc)
    a = await repo.create_delivery_attempt(
        task_id=t.id, target_id=None, adapter="fake", variant="raw",
        max_attempts=3, next_attempt_at=now)
    await session.commit()
    await repo.record_delivery_failure(a.id, last_error="boom",
                                        next_attempt_at=now + timedelta(seconds=60), dead=False)
    await session.commit()
    assert (await repo.list_deliveries_for_task(t.id))[0].status == DeliveryStatus.pending
    await repo.record_delivery_failure(a.id, last_error="boom2", next_attempt_at=None, dead=True)
    await session.commit()
    assert (await repo.list_deliveries_for_task(t.id))[0].status == DeliveryStatus.dead

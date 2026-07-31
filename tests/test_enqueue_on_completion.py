from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo
from vts.delivery.queue import enqueue_deliveries

from _db import make_test_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Postgres-backed async session for the completion-enqueue integration test.

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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_skips_unknown_target_without_raising(session):
    repo = Repo(session)
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    t = Task(
        id=uuid.uuid4(), user_id=u.id, source_url="http://x",
        options={"delivery": [{"deliver_to": "does-not-exist"}]},
        artifact_dir="/tmp/x", status=TaskStatus.completed)
    session.add(t)
    await session.flush()
    n = await enqueue_deliveries(repo, t, max_attempts=5, now=datetime.now(timezone.utc))
    assert n == 0
    assert await repo.list_deliveries_for_task(t.id) == []

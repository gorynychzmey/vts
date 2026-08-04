from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo
from vts.delivery.queue import backoff_seconds, enqueue_deliveries

from _db import make_test_engine


def test_backoff_progression():
    assert backoff_seconds(1, 60, 3600) == 60
    assert backoff_seconds(2, 60, 3600) == 120
    assert backoff_seconds(3, 60, 3600) == 240
    assert backoff_seconds(100, 60, 3600) == 3600  # capped


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Postgres-backed async session for delivery queue integration tests.

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
async def test_enqueue_creates_attempt_per_target(session):
    repo = Repo(session)
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    cred = await repo.create_delivery_credential(
        u.id, name="conn", adapter="fake", config={}, secrets_enc=None)
    target = await repo.create_delivery_target(
        u.id, name="out", adapter="fake", credential_id=cred.id,
        config={"default_variant": "summary"})
    t = Task(
        id=uuid.uuid4(), user_id=u.id, source_url="http://x",
        # deliver_to carries the target's ID, not its name (vts-929).
        options={"delivery": [{"deliver_to": str(target.id)}]},
        artifact_dir="/tmp/x", status=TaskStatus.completed)
    session.add(t)
    await session.flush()
    n = await enqueue_deliveries(repo, t, max_attempts=5, now=datetime.now(timezone.utc))
    assert n == 1
    rows = await repo.list_deliveries_for_task(t.id)
    assert rows[0].adapter == "fake" and rows[0].variant == "summary"

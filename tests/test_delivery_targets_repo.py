from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo

from _db import make_test_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _user(session: AsyncSession) -> User:
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    return u


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_by_name(session):
    repo = Repo(session)
    u = await _user(session)
    t = await repo.create_delivery_target(
        u.id, name="outline-meetings", adapter="outline",
        config={"collection_id": "c1"}, secrets_enc=b"blob")
    await session.commit()
    got = await repo.get_delivery_target_by_name(u.id, "outline-meetings")
    assert got is not None and got.id == t.id
    assert got.secrets_enc == b"blob"


@pytest.mark.asyncio
async def test_update_without_secret_keeps_old(session):
    repo = Repo(session)
    u = await _user(session)
    t = await repo.create_delivery_target(
        u.id, name="t", adapter="outline", config={"a": 1}, secrets_enc=b"old")
    await session.commit()
    updated = await repo.update_delivery_target(
        u.id, t.id, name=None, config={"a": 2}, secrets_enc=None, clear_secrets=False)
    assert updated.config_json == {"a": 2}
    assert updated.secrets_enc == b"old"  # preserved


@pytest.mark.asyncio
async def test_update_clear_secrets(session):
    repo = Repo(session)
    u = await _user(session)
    t = await repo.create_delivery_target(
        u.id, name="t", adapter="outline", config={}, secrets_enc=b"old")
    await session.commit()
    updated = await repo.update_delivery_target(
        u.id, t.id, name=None, config=None, secrets_enc=None, clear_secrets=True)
    assert updated.secrets_enc is None

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Preset, User
from vts.db.repo import Repo

from _db import make_test_engine


def test_preset_model_columns():
    cols = set(Preset.__table__.columns.keys())
    assert {"id", "user_id", "name", "options", "created_at", "updated_at"} <= cols


def test_user_has_default_preset_column():
    assert "default_preset" in set(User.__table__.columns.keys())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Postgres-backed async session for preset repo integration tests.

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


async def _user(session: AsyncSession) -> uuid.UUID:
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    return u.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preset_crud_and_default(session):
    repo = Repo(session)
    uid = await _user(session)
    opts = {"language": None, "audio_only": False, "transcript": True,
            "prompts": [{"source": "system", "id": "summary"}]}
    p = await repo.create_preset(uid, "Mine", opts)
    assert (await repo.list_presets(uid))[0].id == p.id
    assert (await repo.get_preset(uid, p.id)).name == "Mine"
    upd = await repo.update_preset(uid, p.id, name="Renamed", options=None)
    assert upd.name == "Renamed" and upd.options == opts

    # default get/set
    assert await repo.get_user_default_preset(uid) is None
    await repo.set_user_default_preset(uid, {"source": "user", "id": str(p.id)})
    assert await repo.get_user_default_preset(uid) == {"source": "user", "id": str(p.id)}

    # deleting the default preset resets default to None
    assert await repo.delete_preset(uid, p.id) is True
    assert await repo.get_user_default_preset(uid) is None


@pytest.mark.asyncio
async def test_preset_isolation(session):
    repo = Repo(session)
    a = await _user(session)
    b = await _user(session)
    p = await repo.create_preset(a, "A", {})
    assert await repo.get_preset(b, p.id) is None
    assert await repo.delete_preset(b, p.id) is False
    assert await repo.update_preset(b, p.id, name="X", options=None) is None

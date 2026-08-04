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


async def _credential(repo, user_id, *, name="outline-main", config=None, secrets_enc=b"blob"):
    return await repo.create_delivery_credential(
        user_id, name=name, adapter="outline",
        config=config if config is not None else {"base_url": "https://o.example/api"},
        secrets_enc=secrets_enc)


@pytest.mark.asyncio
async def test_create_and_get_by_name(session):
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id)
    t = await repo.create_delivery_target(
        u.id, name="outline-meetings", adapter="outline",
        credential_id=cred.id, config={"collection_id": "c1"})
    await session.commit()
    got = await repo.get_delivery_target_by_name(u.id, "outline-meetings")
    assert got is not None and got.id == t.id
    assert got.credential_id == cred.id


@pytest.mark.asyncio
async def test_update_without_secret_keeps_credential_secret(session):
    """Editing a target must not touch the connection's secret.

    Secrets moved to the credential (vts-929), so a target update has no way
    to disturb them — this pins that separation.
    """
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id, secrets_enc=b"old")
    t = await repo.create_delivery_target(
        u.id, name="t", adapter="outline", credential_id=cred.id, config={"a": 1})
    await session.commit()
    updated = await repo.update_delivery_target(
        u.id, t.id, name=None, config={"a": 2})
    assert updated.config_json == {"a": 2}
    got = await repo.get_delivery_credential(u.id, cred.id)
    assert got.secrets_enc == b"old"


@pytest.mark.asyncio
async def test_update_credential_without_secret_keeps_old(session):
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id, secrets_enc=b"old")
    await session.commit()
    updated = await repo.update_delivery_credential(
        u.id, cred.id, name=None, config={"a": 2},
        secrets_enc=None, clear_secrets=False)
    assert updated.config_json == {"a": 2}
    assert updated.secrets_enc == b"old"  # preserved


@pytest.mark.asyncio
async def test_update_credential_clear_secrets(session):
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id, secrets_enc=b"old")
    await session.commit()
    updated = await repo.update_delivery_credential(
        u.id, cred.id, name=None, config=None,
        secrets_enc=None, clear_secrets=True)
    assert updated.secrets_enc is None


@pytest.mark.asyncio
async def test_count_targets_for_credential(session):
    """The count is what lets a delete be refused with a reason instead of
    surfacing the RESTRICT foreign key as a 500."""
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id)
    assert await repo.count_targets_for_credential(u.id, cred.id) == 0
    for i in range(2):
        await repo.create_delivery_target(
            u.id, name=f"t{i}", adapter="outline",
            credential_id=cred.id, config={"collection_id": f"c{i}"})
    await session.commit()
    assert await repo.count_targets_for_credential(u.id, cred.id) == 2


@pytest.mark.asyncio
async def test_two_targets_share_one_credential(session):
    """The point of the split: two destinations, one endpoint and one token.

    Before vts-929 each target carried its own copy of base_url and the
    token, so rotating it meant editing every row.
    """
    repo = Repo(session)
    u = await _user(session)
    cred = await _credential(repo, u.id, secrets_enc=b"tok")
    a = await repo.create_delivery_target(
        u.id, name="meetings", adapter="outline",
        credential_id=cred.id, config={"collection_id": "c1"})
    b = await repo.create_delivery_target(
        u.id, name="notes", adapter="outline",
        credential_id=cred.id, config={"collection_id": "c2"})
    await session.commit()

    assert a.credential_id == b.credential_id == cred.id
    # Rotating the token is ONE edit and both targets see it.
    await repo.update_delivery_credential(
        u.id, cred.id, name=None, config=None,
        secrets_enc=b"rotated", clear_secrets=False)
    await session.commit()
    for target in (a, b):
        got = await repo.get_delivery_credential(u.id, target.credential_id)
        assert got.secrets_enc == b"rotated"

"""Server-side session records, now backed by Postgres (vts-akf8).

These used to live in Redis, where the TTL did the expiring and a restart
quietly logged everyone out. The store keeps the same three-call shape —
create / lookup / delete — so the callers did not change; what changed is that
the record survives a Redis restart and that the sid is stored as a hash.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import User, UserSession, utcnow
from vts.services import session_store

EMAIL = "alice@example.com"


@pytest_asyncio.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def user(factory):
    async with factory() as session:
        row = User(id=uuid.uuid4(), username=EMAIL)
        session.add(row)
        await session.commit()
    return row


@pytest.mark.asyncio
async def test_create_returns_32_hex_char_sid(factory, user) -> None:
    async with factory() as session:
        sid = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=12345
        )
        await session.commit()

    # token_hex(16) -> 32 hex chars (128 bits). Unchanged from the Redis store:
    # the cookie's shape is not what this migration is about.
    assert len(sid) == 32
    assert all(c in "0123456789abcdef" for c in sid)


@pytest.mark.asyncio
async def test_lookup_returns_record_for_existing_sid(factory, user) -> None:
    async with factory() as session:
        sid = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=12345
        )
        await session.commit()

    async with factory() as session:
        record = await session_store.lookup(session, sid)

    assert record is not None
    assert record.email == EMAIL
    assert record.issued_at == 12345


@pytest.mark.asyncio
async def test_lookup_returns_none_for_missing_sid(factory) -> None:
    async with factory() as session:
        assert await session_store.lookup(session, "deadbeef" * 4) is None


@pytest.mark.asyncio
async def test_delete_removes_the_record(factory, user) -> None:
    async with factory() as session:
        sid = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=12345
        )
        await session.commit()

    async with factory() as session:
        await session_store.delete(session, sid)
        await session.commit()

    async with factory() as session:
        assert await session_store.lookup(session, sid) is None


@pytest.mark.asyncio
async def test_delete_missing_sid_is_safe(factory) -> None:
    """logout-after-expiry must not raise."""
    async with factory() as session:
        await session_store.delete(session, "deadbeef" * 4)
        await session.commit()


@pytest.mark.asyncio
async def test_the_raw_sid_is_never_stored(factory, user) -> None:
    """The sid is a bearer credential: whoever reads it is logged in as its owner.

    Redis held it in plain text behind a TTL; the database is dumped, backed up
    and kept, so the row stores a SHA-256 of the sid the same way api_tokens
    stores token_hash.
    """
    async with factory() as session:
        sid = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=12345
        )
        await session.commit()

    async with factory() as session:
        rows = list(await session.scalars(sa.select(UserSession)))

    assert len(rows) == 1
    assert sid not in rows[0].sid_hash
    assert rows[0].sid_hash == session_store.hash_sid(sid)
    assert len(rows[0].sid_hash) == 64


@pytest.mark.asyncio
async def test_expired_session_does_not_resolve(factory, user) -> None:
    """The TTL used to do this; now an explicit expires_at filter must."""
    async with factory() as session:
        sid = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=12345
        )
        await session.commit()

    async with factory() as session:
        await session.execute(
            sa.update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()

    async with factory() as session:
        assert await session_store.lookup(session, sid) is None


@pytest.mark.asyncio
async def test_purge_expired_removes_only_expired_rows(factory, user) -> None:
    """Redis reclaimed expired keys on its own; in the database we must sweep,
    or the table grows for the life of the deployment."""
    async with factory() as session:
        live = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=1
        )
        stale = await session_store.create(
            session, email=EMAIL, ttl_seconds=3600, issued_at=2
        )
        await session.commit()

    async with factory() as session:
        await session.execute(
            sa.update(UserSession)
            .where(UserSession.sid_hash == session_store.hash_sid(stale))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()

    async with factory() as session:
        removed = await session_store.purge_expired(session)
        await session.commit()

    assert removed == 1
    async with factory() as session:
        assert await session_store.lookup(session, live) is not None
        assert await session_store.lookup(session, stale) is None


@pytest.mark.asyncio
async def test_deleting_the_user_takes_their_sessions(factory, user) -> None:
    async with factory() as session:
        await session_store.create(session, email=EMAIL, ttl_seconds=3600, issued_at=1)
        await session.commit()

    async with factory() as session:
        await session.execute(sa.delete(User).where(User.username == EMAIL))
        await session.commit()

    async with factory() as session:
        assert list(await session.scalars(sa.select(UserSession))) == []


@pytest.mark.asyncio
async def test_create_for_unknown_email_returns_none(factory) -> None:
    """Callers create the user first (auth_routes does). If that ever stops
    being true, the store must not invent a session with no owner."""
    async with factory() as session:
        sid = await session_store.create(
            session, email="nobody@example.com", ttl_seconds=3600, issued_at=1
        )
        await session.commit()

    assert sid is None

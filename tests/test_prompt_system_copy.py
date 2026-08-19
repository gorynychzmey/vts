"""Behaviour of the per-user copy of a vendor system prompt (vts-kujy)."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import make_test_engine
from vts.db.base import Base
from vts.db.models import Prompt, User


@pytest_asyncio.fixture
async def factory():
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_copy_is_created_without_an_updated_at(factory) -> None:
    """`updated_at` answers "when did the user change this?" — NULL means never.

    A plain `session.add()` with `updated_at=None` would silently pick up the
    column default and land the row with the current time, which is why the
    column carries no default at all. This test is the guard: if someone
    reinstates `default=utcnow`, the NULL disappears and the startup refresh
    stops recognising untouched copies.
    """
    async with factory() as session:
        user_id = uuid.uuid4()
        session.add(User(id=user_id, username="prompt-system-copy@example.invalid"))
        await session.commit()

        session.add(
            Prompt(
                user_id=user_id,
                name="Summary",
                system_prompt="vendor text",
                is_system=True,
                created_at=datetime.now(),
                updated_at=None,
            )
        )
        await session.commit()

        row = (
            await session.scalars(sa.select(Prompt).where(Prompt.user_id == user_id))
        ).one()
        assert row.is_system is True
        assert row.updated_at is None, "a fresh system copy must carry no edit timestamp"

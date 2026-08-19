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


@pytest.mark.asyncio
async def test_create_prompt_stamps_a_user_prompt_and_not_a_system_copy(factory) -> None:
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        user_id = uuid.uuid4()
        session.add(User(id=user_id, username="prompt-create-stamps@example.invalid"))
        await session.commit()

        mine = await repo.create_prompt(user_id, "Mine", "body")
        vendor = await repo.create_prompt(user_id, "Summary", "vendor", is_system=True)
        await session.commit()

    assert mine.updated_at is not None, "a prompt the user wrote is edited by definition"
    assert mine.is_system is False
    assert vendor.updated_at is None, "a vendor copy has not been touched by the user"
    assert vendor.is_system is True


@pytest.mark.asyncio
async def test_update_prompt_stamps_updated_at(factory) -> None:
    """Editing is what puts a timestamp on a system copy."""
    from vts.db.repo import Repo

    async with factory() as session:
        repo = Repo(session)
        user_id = uuid.uuid4()
        session.add(User(id=user_id, username="prompt-update-stamps@example.invalid"))
        await session.commit()

        vendor = await repo.create_prompt(user_id, "Summary", "vendor", is_system=True)
        await session.commit()
        assert vendor.updated_at is None

        await repo.update_prompt(
            user_id, vendor.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    assert vendor.updated_at is not None, "an edit must be recorded"
    assert vendor.system_prompt == "my own wording"

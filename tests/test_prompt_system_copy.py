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


@pytest.mark.asyncio
async def test_get_or_create_reads_the_file_once_then_reuses_the_row(
    factory, tmp_path
) -> None:
    """The copy is made on first use, not at signup.

    A task runs in the worker, so a user who has never opened the UI still has
    no row when their first summary starts. Creating on demand also means a row
    lost for any reason simply comes back.
    """
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=user_id, username="prompt-lazy-copy@example.invalid"))
        await session.commit()

        first = await get_or_create_system_prompt(session, user_id, tmp_path)
        await session.commit()
        first_id = first.id

    assert first.system_prompt == "vendor text"
    assert first.is_system is True
    assert first.updated_at is None

    # A second call must not make a second copy.
    async with factory() as session:
        again = await get_or_create_system_prompt(session, user_id, tmp_path)
        await session.commit()
    assert again.id == first_id

    async with factory() as session:
        rows = (
            await session.scalars(sa.select(Prompt).where(Prompt.user_id == user_id))
        ).all()
    assert len(rows) == 1, "the vendor copy must exist exactly once per user"


@pytest.mark.asyncio
async def test_get_or_create_returns_an_edited_copy_unchanged(factory, tmp_path) -> None:
    """Once the user has edited it, the file is no longer consulted."""
    from vts.db.repo import Repo
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=user_id, username="prompt-lazy-copy-edited@example.invalid"))
        await session.commit()

        row = await get_or_create_system_prompt(session, user_id, tmp_path)
        await Repo(session).update_prompt(
            user_id, row.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    (tmp_path / "global_prompt.md").write_text("a newer vendor text", encoding="utf-8")

    async with factory() as session:
        again = await get_or_create_system_prompt(session, user_id, tmp_path)
    assert again.system_prompt == "my own wording"

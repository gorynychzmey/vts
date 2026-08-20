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
async def test_get_or_create_does_not_attempt_an_insert_on_the_reuse_path(
    factory, tmp_path
) -> None:
    """The reuse path must short-circuit on the existing-row SELECT alone.

    Without that check, a second call would still attempt `create_prompt`,
    hit the partial unique index, and recover via the `IntegrityError`
    handler — landing on the same row and passing a test that only counts
    rows. Counting INSERT *attempts* instead is what actually distinguishes
    "found the row and skipped creation" from "tried to create it and lost
    the race but recovered".
    """
    from unittest.mock import patch

    from vts.db.repo import Repo
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=user_id, username="prompt-lazy-no-reinsert@example.invalid"))
        await session.commit()

        first = await get_or_create_system_prompt(session, user_id, tmp_path)
        await session.commit()
        first_id = first.id

    create_attempts = 0
    real_create_prompt = Repo.create_prompt

    async def counting_create_prompt(self, *args, **kwargs):
        nonlocal create_attempts
        create_attempts += 1
        return await real_create_prompt(self, *args, **kwargs)

    async with factory() as session:
        with patch.object(Repo, "create_prompt", counting_create_prompt):
            again = await get_or_create_system_prompt(session, user_id, tmp_path)
            await session.commit()

    assert again.id == first_id
    assert create_attempts == 0, "the reuse path must not attempt to insert at all"


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


@pytest.mark.asyncio
async def test_get_or_create_survives_a_race_without_losing_the_callers_work(
    factory, tmp_path
) -> None:
    """Losing the INSERT race must not roll back the caller's own pending work.

    `get_or_create_system_prompt` is meant to be called mid-transaction by
    callers (API, worker pipeline) that already hold other changes in the same
    session — a worker task typically has status/progress edits pending. A
    bare `session.rollback()` after a lost race would discard the *whole*
    transaction, not just the failed INSERT, silently dropping that work. This
    reproduces the race with two sessions and asserts the caller's own edit
    survives.
    """
    from unittest.mock import patch

    from vts.db.repo import Repo
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as setup:
        setup.add(User(id=user_id, username="prompt-race@example.invalid"))
        await setup.commit()

    winner_id: uuid.UUID | None = None
    real_scalars = AsyncSession.scalars

    async with factory() as loser:
        # The caller already has unrelated work pending in this session, the
        # way a worker task would have its own status/progress edits pending
        # before it ever asks for the system prompt.
        await Repo(loser).create_prompt(user_id, "Mine", "caller's work")

        async def scalars_that_lets_another_session_win_first(self, *args, **kwargs):
            # Called for `loser`'s own lookup SELECT inside
            # `get_or_create_system_prompt`. Right after it returns "no row
            # yet", another session creates and commits the row — the exact
            # race window the brief describes: a second caller wins between
            # our SELECT and our INSERT. Only `loser`'s own call triggers
            # this — the winner's internal SELECT must run unpatched, or the
            # two sessions would spawn each other forever.
            nonlocal winner_id
            result = await real_scalars(self, *args, **kwargs)
            if self is loser and winner_id is None:
                async with factory() as winner:
                    winner_row = await get_or_create_system_prompt(winner, user_id, tmp_path)
                    await winner.commit()
                    winner_id = winner_row.id
            return result

        with patch.object(
            AsyncSession, "scalars", scalars_that_lets_another_session_win_first
        ):
            # `loser` now loses the INSERT race on the partial unique index.
            again = await get_or_create_system_prompt(loser, user_id, tmp_path)
        await loser.commit()

    assert again.id == winner_id, "the loser must return the winner's row"

    async with factory() as session:
        mine = (
            await session.scalars(
                sa.select(Prompt).where(Prompt.user_id == user_id, Prompt.name == "Mine")
            )
        ).one_or_none()
    assert mine is not None, "the caller's own pending work must survive the lost race"
    assert mine.system_prompt == "caller's work"


def test_vendor_text_falls_back_on_an_empty_file(tmp_path) -> None:
    """An empty (e.g. truncated by a failed deploy write) file must not win.

    `load_prompt` only substitutes the fallback when the file is *missing*.
    An empty file exists, so without an explicit check `vendor_text` would
    return `""`, and every new user would get a copy with an empty system
    prompt forever — the restore path just deletes the row and re-reads the
    same empty file.
    """
    from vts.services.system_prompt import _FALLBACK, vendor_text

    (tmp_path / "global_prompt.md").write_text("", encoding="utf-8")

    assert vendor_text(tmp_path) == _FALLBACK


def test_vendor_text_falls_back_on_an_unreadable_file(tmp_path) -> None:
    """A file the process cannot read (bad permissions, bad mount) must not
    raise — it must fall back exactly like a missing file."""
    import os

    from vts.services.system_prompt import _FALLBACK, vendor_text

    path = tmp_path / "global_prompt.md"
    path.write_text("vendor text", encoding="utf-8")
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that bypasses file permissions (e.g. root)")
        assert vendor_text(tmp_path) == _FALLBACK
    finally:
        path.chmod(0o644)


@pytest.mark.asyncio
async def test_pipeline_uses_the_users_edited_copy(factory, tmp_path) -> None:
    """The summary must run on what the user wrote, not on the file.

    This drives `SystemPromptSource.load_text` — the pipeline's own call site —
    rather than the service underneath it, so that reverting the call site to
    `load_prompt` fails here. A service-level assertion would keep passing
    with the pipeline still reading the file.
    """
    from types import SimpleNamespace

    from vts.db.repo import Repo
    from vts.pipeline.steps.summarization import SystemPromptSource
    from vts.services.system_prompt import get_or_create_system_prompt

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=user_id, username="prompt-pipeline-edited@example.invalid"))
        await session.commit()

        row = await get_or_create_system_prompt(session, user_id, tmp_path)
        await Repo(session).update_prompt(
            user_id, row.id, name=None, system_prompt="my own wording"
        )
        await session.commit()

    ctx = SimpleNamespace(
        settings=SimpleNamespace(prompts_dir=tmp_path),
        session_factory=factory,
    )
    text = await SystemPromptSource().load_text(ctx, "summary", "en", str(user_id))
    assert "my own wording" in text
    assert "vendor text" not in text


@pytest.mark.asyncio
async def test_pipeline_creates_the_copy_on_a_first_summary(factory, tmp_path) -> None:
    """A user whose first summary runs before they ever open the UI.

    The worker is then the first caller, so the copy has to be made there —
    and the run must use the vendor text, not fail for the missing row.
    """
    from types import SimpleNamespace

    from vts.pipeline.steps.summarization import SystemPromptSource

    (tmp_path / "global_prompt.md").write_text("vendor text", encoding="utf-8")
    user_id = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=user_id, username="prompt-pipeline-first@example.invalid"))
        await session.commit()

    ctx = SimpleNamespace(
        settings=SimpleNamespace(prompts_dir=tmp_path),
        session_factory=factory,
    )
    text = await SystemPromptSource().load_text(ctx, "summary", "en", str(user_id))
    assert "vendor text" in text

    async with factory() as session:
        rows = (
            await session.scalars(
                sa.select(Prompt).where(Prompt.user_id == user_id, Prompt.is_system)
            )
        ).all()
    assert len(rows) == 1, "the worker must persist the copy it just made"


@pytest.mark.asyncio
async def test_refresh_rewrites_untouched_copies_and_spares_edited_ones(
    factory, tmp_path
) -> None:
    """A newly shipped prompt has to reach users who already have a copy.

    Inferring "untouched" by comparing the copy to the current file does not
    work: a copy made from v1 and never touched does not match v2 either, so
    every untouched copy would read as edited exactly when the refresh is
    needed. The record — `updated_at IS NULL` — survives any number of
    releases, which this test pins by shipping *two* new versions.
    """
    from vts.db.repo import Repo
    from vts.services.system_prompt import (
        get_or_create_system_prompt,
        refresh_untouched_system_prompts,
    )

    (tmp_path / "global_prompt.md").write_text("v1", encoding="utf-8")
    untouched_user = uuid.uuid4()
    editing_user = uuid.uuid4()

    async with factory() as session:
        session.add(User(id=untouched_user, username="prompt-refresh-untouched@example.invalid"))
        session.add(User(id=editing_user, username="prompt-refresh-edited@example.invalid"))
        await session.flush()
        await get_or_create_system_prompt(session, untouched_user, tmp_path)
        edited = await get_or_create_system_prompt(session, editing_user, tmp_path)
        await Repo(session).update_prompt(
            editing_user, edited.id, name=None, system_prompt="mine"
        )
        await session.commit()

    (tmp_path / "global_prompt.md").write_text("v2", encoding="utf-8")
    async with factory() as session:
        refreshed, skipped = await refresh_untouched_system_prompts(session, tmp_path)
        await session.commit()
    assert (refreshed, skipped) == (1, 1)

    # Second release: the untouched copy now holds v2, which matches neither
    # v1 nor v3 — comparing text would call it edited from here on.
    (tmp_path / "global_prompt.md").write_text("v3", encoding="utf-8")
    async with factory() as session:
        refreshed, skipped = await refresh_untouched_system_prompts(session, tmp_path)
        await session.commit()
    assert (refreshed, skipped) == (1, 1)

    async with factory() as session:
        rows = {
            r.user_id: r
            for r in (await session.scalars(sa.select(Prompt).where(Prompt.is_system))).all()
        }
    assert rows[untouched_user].system_prompt == "v3"
    assert rows[untouched_user].updated_at is None, "a refresh is not a user edit"
    assert rows[editing_user].system_prompt == "mine"

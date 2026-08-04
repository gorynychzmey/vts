"""Filtering the task list by name / date / type (vts-rhx, VOS-84b).

The filters narrow the SAME cursor query pagination uses, so the tests below
also pin that they do not disturb the cursor: a filtered page must still be
orderable and pageable exactly as an unfiltered one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo

from _db import make_test_engine

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = make_test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _user(session) -> User:
    u = User(id=uuid.uuid4(), username=f"u-{uuid.uuid4().hex[:8]}")
    session.add(u)
    await session.flush()
    return u


async def _task(session, user, *, url, title=None, minutes=0):
    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        source_url=url,
        source_title=title,
        status=TaskStatus.completed,
        artifact_dir="/tmp/x",
        created_at=BASE + timedelta(minutes=minutes),
    )
    session.add(t)
    await session.flush()
    return t


async def _page(repo, user, **kwargs):
    return await repo.list_tasks_page(user.id, limit=50, **kwargs)


@pytest.mark.asyncio
async def test_search_matches_title_and_url(session):
    """Both fields, by explicit decision: a user may remember either the
    title they gave a task or a fragment of the link."""
    repo = Repo(session)
    u = await _user(session)
    by_title = await _task(session, u, url="https://example.com/a", title="Board meeting")
    by_url = await _task(session, u, url="https://youtube.com/watch?v=meeting42", minutes=1)
    await _task(session, u, url="https://example.com/other", title="Groceries", minutes=2)

    found = {t.id for t in await _page(repo, u, q="meeting")}
    assert found == {by_title.id, by_url.id}


@pytest.mark.asyncio
async def test_search_is_case_insensitive(session):
    repo = Repo(session)
    u = await _user(session)
    t = await _task(session, u, url="https://e.com/1", title="Quarterly Review")
    assert {x.id for x in await _page(repo, u, q="quarterly review")} == {t.id}


@pytest.mark.asyncio
async def test_search_escapes_like_wildcards(session):
    """A literal % or _ must match itself, not act as a pattern.

    Without escaping, searching "100%" would match every task, and "a_b"
    would match "axb" — the user would silently get the wrong rows.
    """
    repo = Repo(session)
    u = await _user(session)
    literal = await _task(session, u, url="https://e.com/1", title="Battery at 100% now")
    await _task(session, u, url="https://e.com/2", title="Nothing special", minutes=1)
    await _task(session, u, url="https://e.com/3", title="axb underscore-ish", minutes=2)
    under = await _task(session, u, url="https://e.com/4", title="a_b literal", minutes=3)

    assert {t.id for t in await _page(repo, u, q="100%")} == {literal.id}
    assert {t.id for t in await _page(repo, u, q="a_b")} == {under.id}


@pytest.mark.asyncio
async def test_filter_by_source_type(session):
    repo = Repo(session)
    u = await _user(session)
    uploaded = await _task(session, u, url="file://recording.m4a", title="Upload")
    linked = await _task(session, u, url="https://youtube.com/x", title="Link", minutes=1)

    assert {t.id for t in await _page(repo, u, source_type="file")} == {uploaded.id}
    assert {t.id for t in await _page(repo, u, source_type="url")} == {linked.id}
    # Omitting the filter returns both — the default must not narrow anything.
    assert len(await _page(repo, u)) == 2


@pytest.mark.asyncio
async def test_filter_by_created_range_is_inclusive(session):
    repo = Repo(session)
    u = await _user(session)
    first = await _task(session, u, url="https://e.com/1", minutes=0)
    second = await _task(session, u, url="https://e.com/2", minutes=10)
    third = await _task(session, u, url="https://e.com/3", minutes=20)

    got = {t.id for t in await _page(
        repo, u,
        created_from=BASE + timedelta(minutes=10),
        created_to=BASE + timedelta(minutes=20),
    )}
    assert got == {second.id, third.id}

    only_early = {t.id for t in await _page(
        repo, u, created_to=BASE + timedelta(minutes=5))}
    assert only_early == {first.id}


@pytest.mark.asyncio
async def test_filters_combine(session):
    repo = Repo(session)
    u = await _user(session)
    await _task(session, u, url="file://meeting.m4a", title="Meeting notes", minutes=0)
    wanted = await _task(session, u, url="file://meeting2.m4a", title="Meeting notes", minutes=30)
    await _task(session, u, url="https://e.com/meeting", title="Meeting notes", minutes=30)

    got = await _page(
        repo, u, q="meeting", source_type="file",
        created_from=BASE + timedelta(minutes=15),
    )
    assert [t.id for t in got] == [wanted.id]


@pytest.mark.asyncio
async def test_filter_does_not_disturb_the_cursor(session):
    """The filters narrow the same query the cursor pages over, so a filtered
    result set must still page correctly rather than skipping or repeating."""
    repo = Repo(session)
    u = await _user(session)
    matching = [
        await _task(session, u, url=f"file://m{i}.m4a", title="keep", minutes=i * 2)
        for i in range(5)
    ]
    for i in range(5):  # interleaved noise that the filter must exclude
        await _task(session, u, url=f"https://e.com/{i}", title="drop", minutes=i * 2 + 1)

    first = await repo.list_tasks_page(u.id, limit=2, q="keep", source_type="file")
    assert len(first) == 2
    tail = first[-1]
    second = await repo.list_tasks_page(
        u.id, limit=2, q="keep", source_type="file",
        before=(tail.created_at, tail.id),
    )
    ids = [t.id for t in first + second]
    assert len(set(ids)) == 4, "cursor paging repeated or skipped a filtered row"
    assert set(ids) <= {t.id for t in matching}


@pytest.mark.asyncio
async def test_filters_are_scoped_to_the_user(session):
    repo = Repo(session)
    mine = await _user(session)
    theirs = await _user(session)
    await _task(session, theirs, url="https://e.com/secret", title="meeting")
    assert await _page(repo, mine, q="meeting") == []

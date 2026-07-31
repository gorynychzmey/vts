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


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _seed(session, user_id, n):
    """Insert n tasks, task i created at BASE + i minutes (task 0 oldest)."""
    tasks = []
    for i in range(n):
        t = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url=f"https://example.com/{i}",
            status=TaskStatus.completed,
            artifact_dir=f"/tmp/{i}",
            created_at=BASE + timedelta(minutes=i),
        )
        session.add(t)
        tasks.append(t)
    await session.flush()
    return tasks


@pytest_asyncio.fixture
async def user(session):
    u = User(username="u1")
    session.add(u)
    await session.flush()
    return u


async def test_first_page_desc_returns_newest_first(session, user):
    await _seed(session, user.id, 5)
    repo = Repo(session)
    page = await repo.list_tasks_page(user.id, limit=2)
    assert [t.source_url for t in page] == [
        "https://example.com/4",
        "https://example.com/3",
    ]


async def test_before_pages_downward(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    tail = tasks[3]  # created at minute 3
    page = await repo.list_tasks_page(
        user.id, before=(tail.created_at, tail.id), limit=2
    )
    assert [t.source_url for t in page] == [
        "https://example.com/2",
        "https://example.com/1",
    ]


async def test_after_asc_returns_adjacent_newer(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    head = tasks[1]  # created at minute 1
    page = await repo.list_tasks_page(
        user.id, after=(head.created_at, head.id), order="asc", limit=2
    )
    # ascending, nearest to head first
    assert [t.source_url for t in page] == [
        "https://example.com/2",
        "https://example.com/3",
    ]


async def test_interval_returns_only_in_between(session, user):
    tasks = await _seed(session, user.id, 5)
    repo = Repo(session)
    lo, hi = tasks[0], tasks[4]
    page = await repo.list_tasks_page(
        user.id,
        after=(lo.created_at, lo.id),
        before=(hi.created_at, hi.id),
        limit=10,
    )
    assert [t.source_url for t in page] == [
        "https://example.com/3",
        "https://example.com/2",
        "https://example.com/1",
    ]


async def test_composite_cursor_breaks_equal_created_at(session, user):
    # Two tasks share created_at; cursor on the larger id must not skip
    # or duplicate the smaller-id one.
    ts = BASE
    a = Task(id=uuid.UUID(int=1), user_id=user.id, source_url="a",
             status=TaskStatus.completed, artifact_dir="/tmp/a", created_at=ts)
    b = Task(id=uuid.UUID(int=2), user_id=user.id, source_url="b",
             status=TaskStatus.completed, artifact_dir="/tmp/b", created_at=ts)
    session.add_all([a, b])
    await session.flush()
    repo = Repo(session)
    # desc order → (ts, id=2) first, then (ts, id=1)
    first = await repo.list_tasks_page(user.id, limit=1)
    assert first[0].id == uuid.UUID(int=2)
    nxt = await repo.list_tasks_page(
        user.id, before=(ts, uuid.UUID(int=2)), limit=10
    )
    assert [t.id for t in nxt] == [uuid.UUID(int=1)]


async def test_status_filter_narrows_and_cursor_still_pages(session, user):
    # 4 tasks: alternate completed/queued, minute 0..3 (task 3 newest).
    rows = []
    for i in range(4):
        t = Task(
            id=uuid.uuid4(), user_id=user.id, source_url=f"https://example.com/{i}",
            artifact_dir=f"/tmp/{i}",
            status=TaskStatus.completed if i % 2 == 0 else TaskStatus.queued,
            created_at=BASE + timedelta(minutes=i),
        )
        session.add(t)
        rows.append(t)
    await session.flush()
    repo = Repo(session)
    # completed tasks are i=0 (min0) and i=2 (min2); newest-first -> [2, 0]
    page = await repo.list_tasks_page(user.id, limit=1, status=TaskStatus.completed)
    assert [t.source_url for t in page] == ["https://example.com/2"]
    tail = page[-1]
    page2 = await repo.list_tasks_page(
        user.id, before=(tail.created_at, tail.id), limit=10, status=TaskStatus.completed
    )
    assert [t.source_url for t in page2] == ["https://example.com/0"]
    # no queued leaked in either page
    assert all(
        "example.com/1" not in t.source_url and "example.com/3" not in t.source_url
        for t in page + page2
    )

"""Recording filters belong to the QUERY, not to the loaded page.

vts-jv2n: paging shrank the client-side filter's universe from 200 rows to 30
without saying so, and the count beside the heading reported matches among the
loaded thirty. A user with 40 recordings searched by name and was told the
recording does not exist.

vts-7d0y: the same selection was being built three times — Repo, the HTTP
endpoint, and the MCP tool, the last two with their own WHERE clauses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, User
from vts.db.repo import Repo

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000b7")


@pytest.fixture
async def db_session():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(User(id=_USER, username="filter-tester"))
        await s.commit()
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(session, user_id: uuid.UUID) -> list[Recording]:
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    made = []
    for i, title in enumerate(["Планёрка", "Интервью с Яной", "Лекция"]):
        rec = Recording(
            id=uuid.uuid4(),
            user_id=user_id,
            title=title,
            artifact_dir=f"/tmp/vts-test/{i}",
            # A recording without text is not listed (vts-lzad).
            transcript_path=f"/tmp/vts-test/{i}/transcript.txt",
            created_at=base + timedelta(days=i),
            listed_at=base + timedelta(days=i),
        )
        session.add(rec)
        made.append(rec)
    await session.flush()
    return made


@pytest.mark.asyncio
async def test_name_filter_runs_in_the_query_not_over_one_page(db_session):
    """The whole point: a match on page 2 must still be found from page 1."""
    user_id = _USER
    await _seed(db_session, user_id)
    repo = Repo(db_session)
    # limit=1 makes the "only what is loaded" bug impossible to miss.
    found = await repo.list_recordings(user_id, limit=1, offset=0, q="Лекция")
    assert [r.title for r in found] == ["Лекция"]


@pytest.mark.asyncio
async def test_count_matches_the_filter_not_the_corpus(db_session):
    """A count that ignores the filter reads as "showing 1 of 3"."""
    user_id = _USER
    await _seed(db_session, user_id)
    repo = Repo(db_session)
    assert await repo.count_recordings(user_id, q="Лекция") == 1
    assert await repo.count_recordings(user_id) == 3


@pytest.mark.asyncio
async def test_name_filter_is_case_insensitive_and_partial(db_session):
    user_id = _USER
    await _seed(db_session, user_id)
    repo = Repo(db_session)
    found = await repo.list_recordings(user_id, limit=50, q="яно")
    assert [r.title for r in found] == ["Интервью с Яной"]


@pytest.mark.asyncio
async def test_date_bounds_are_inclusive_and_independent(db_session):
    """Either bound alone is a valid request; a recording made on the "to"
    day must not be cut off at midnight."""
    user_id = _USER
    made = await _seed(db_session, user_id)
    repo = Repo(db_session)
    cut = made[1].created_at
    since = await repo.list_recordings(user_id, limit=50, created_from=cut)
    until = await repo.list_recordings(user_id, limit=50, created_to=cut)
    assert len(since) == 2 and len(until) == 2      # the boundary row is in both
    assert len(since) + len(until) == 4             # 3 rows, one counted twice


@pytest.mark.asyncio
async def test_recording_without_transcript_is_not_listed_nor_counted(db_session):
    """vts-lzad: nothing to open means nothing to list, and the count agrees."""
    made = await _seed(db_session, _USER)
    empty = Recording(
        id=uuid.uuid4(), user_id=_USER, title="No text yet",
        artifact_dir="/tmp/vts-test/empty",
    )
    db_session.add(empty)
    await db_session.flush()
    repo = Repo(db_session)
    listed = await repo.list_recordings(_USER, limit=50)
    assert empty.id not in {r.id for r in listed}
    assert {r.id for r in listed} == {r.id for r in made}
    assert await repo.count_recordings(_USER) == 3


@pytest.mark.asyncio
async def test_recording_without_transcript_is_still_fetchable_by_id(db_session):
    empty = Recording(
        id=uuid.uuid4(), user_id=_USER, title="No text yet",
        artifact_dir="/tmp/vts-test/empty",
    )
    db_session.add(empty)
    await db_session.flush()
    assert await Repo(db_session).get_recording_for_user(_USER, empty.id) is not None

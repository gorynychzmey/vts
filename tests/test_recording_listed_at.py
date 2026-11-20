"""`listed_at`: when a recording entered the library (vts-lzad).

The library is ordered by this moment rather than by creation, so a recording
that gets its text later lands on top, and cursor pages stay stable when new
ones arrive.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Recording, User, utcnow
from vts.db.repo import Repo
from vts.services.recording_backfill import backfill_listed_at

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


@pytest.fixture
async def factory():
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with f() as s:
        s.add(User(id=_USER, username="tester"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _task(repo, name: str):
    task = await repo.create_task(
        user_id=_USER, source_url=f"https://example.com/{name}",
        options={}, artifact_dir=f"/tmp/listed-{name}",
    )
    task.source_title = name
    return task


@pytest.mark.asyncio
async def test_listed_at_set_on_first_transcript_and_never_moves(factory):
    async with factory() as session:
        repo = Repo(session)
        task = await _task(repo, "a")
        recording = await repo.recording_for_task(task.id)
        assert recording.listed_at is None

        await repo.set_task_content(task, transcript_path="/tmp/listed-a/t1.txt")
        first = recording.listed_at
        assert first is not None

        await repo.set_task_content(
            task, transcript_path="/tmp/listed-a/t2.txt", summary_path="/tmp/listed-a/s.md"
        )
        await repo.upsert_recording_for_task(task)
        assert recording.listed_at == first


@pytest.mark.asyncio
async def test_library_orders_by_listed_at_not_created_at(factory):
    async with factory() as session:
        repo = Repo(session)
        a = await _task(repo, "a")
        b = await _task(repo, "b")
        await repo.set_task_content(b, transcript_path="/tmp/listed-b/t.txt")
        await repo.set_task_content(a, transcript_path="/tmp/listed-a/t.txt")
        ra = await repo.recording_for_task(a.id)
        rb = await repo.recording_for_task(b.id)
        # created_at order is a, b; entry order is b, a, so newest-first is a, b.
        got = await repo.list_recordings(_USER)
        assert [r.id for r in got] == [ra.id, rb.id]


@pytest.mark.asyncio
async def test_cursor_pages_do_not_overlap_after_insert_on_top(factory):
    async with factory() as session:
        repo = Repo(session)
        ids = []
        for name in ("a", "b", "c"):
            t = await _task(repo, name)
            await repo.set_task_content(t, transcript_path=f"/tmp/listed-{name}/t.txt")
            ids.append((await repo.recording_for_task(t.id)).id)
        await session.flush()

        page1 = await repo.list_recordings(_USER, limit=2)
        assert [r.id for r in page1] == [ids[2], ids[1]]

        t = await _task(repo, "d")
        await repo.set_task_content(t, transcript_path="/tmp/listed-d/t.txt")
        new_id = (await repo.recording_for_task(t.id)).id

        tail = page1[-1]
        page2 = await repo.list_recordings(
            _USER, limit=2, before=(tail.listed_at, tail.id)
        )
        assert [r.id for r in page2] == [ids[0]]

        head = page1[0]
        newer = await repo.list_recordings(
            _USER, limit=10, after=(head.listed_at, head.id), order="asc"
        )
        assert [r.id for r in newer] == [new_id]
        # count ignores cursors
        assert await repo.count_recordings(_USER) == 4


@pytest.mark.asyncio
async def test_task_ids_filter_returns_that_tasks_recording(authed_app, client):
    from tests.conftest import _TEST_USER_ID

    _app, session_factory = authed_app
    async with session_factory() as session:
        repo = Repo(session)
        uid = uuid.UUID(_TEST_USER_ID)
        tasks = []
        for name in ("x", "y"):
            t = await repo.create_task(
                user_id=uid, source_url=f"https://example.com/{name}",
                options={}, artifact_dir=f"/tmp/listed-{name}",
            )
            await repo.set_task_content(t, transcript_path=f"/tmp/listed-{name}/t.txt")
            tasks.append(t)
        await session.commit()
        wanted = tasks[0].id

    r = await client.get("/api/recordings", params={"task_id": str(wanted)})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["source_task_id"] for i in items] == [str(wanted)]
    assert items[0]["listed_at"] is not None


@pytest.mark.asyncio
async def test_half_cursor_pair_is_422(authed_app, client):
    r = await client.get("/api/recordings", params={"before_ts": "2026-01-01T00:00:00Z"})
    assert r.status_code == 422
    r = await client.get("/api/recordings", params={"after_id": str(uuid.uuid4())})
    assert r.status_code == 422
    r = await client.get("/api/recordings", params={"order": "sideways"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_http_cursor_pages(authed_app, client):
    from tests.conftest import _TEST_USER_ID

    _app, session_factory = authed_app
    async with session_factory() as session:
        repo = Repo(session)
        uid = uuid.UUID(_TEST_USER_ID)
        for name in ("p", "q", "r"):
            t = await repo.create_task(
                user_id=uid, source_url=f"https://example.com/{name}",
                options={}, artifact_dir=f"/tmp/listed-{name}",
            )
            await repo.set_task_content(t, transcript_path=f"/tmp/listed-{name}/t.txt")
        await session.commit()

    page1 = (await client.get("/api/recordings", params={"limit": 2})).json()["items"]
    assert len(page1) == 2
    tail = page1[-1]
    page2 = await client.get(
        "/api/recordings",
        params={"limit": 2, "before_ts": tail["listed_at"], "before_id": tail["id"]},
    )
    assert page2.status_code == 200, page2.text
    assert len(page2.json()["items"]) == 1


@pytest.mark.asyncio
async def test_listed_at_backfill_uses_created_at_for_recordings_with_text(factory):
    created = utcnow() - timedelta(days=3)
    async with factory() as session:
        with_text = Recording(
            id=uuid.uuid4(), user_id=_USER, title="t", artifact_dir="/tmp/x",
            transcript_path="/tmp/x/t.txt", created_at=created,
        )
        without = Recording(
            id=uuid.uuid4(), user_id=_USER, title="n", artifact_dir="/tmp/y",
            created_at=created,
        )
        session.add_all([with_text, without])
        await session.commit()
        with_id, without_id = with_text.id, without.id

    async with factory() as s:
        async with s.bind.begin() as conn:
            changed = await conn.run_sync(backfill_listed_at)
    assert changed == 1

    async with factory() as s:
        rows = {r.id: r for r in (await s.execute(select(Recording))).scalars().all()}
    assert rows[with_id].listed_at == created
    assert rows[without_id].listed_at is None

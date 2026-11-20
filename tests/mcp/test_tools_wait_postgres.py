"""wait_for_task holds one session for the whole wait; its re-checks must see
what a worker commits meanwhile (real Postgres: the identity map is the issue)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from tests.mcp.conftest import FakeRedisWithPubSub, FakeUser
from vts.db.base import Base
from vts.db.models import Recording, Task, TaskStatus, User
from vts.db.repo import Repo
from vts.mcp import tools as tools_mod

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


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


async def _seed(factory, tmp_path):
    async with factory() as s:
        t = Task(id=uuid.uuid4(), user_id=_USER, source_url="x", options={},
                 artifact_dir=str(tmp_path), status=TaskStatus.running)
        s.add(t)
        await s.flush()
        s.add(Recording(user_id=_USER, source_task_id=t.id, artifact_dir=str(tmp_path)))
        await s.commit()
        return t.id


@pytest.mark.parametrize("until", ["summary", "done"])
async def test_a_later_commit_reaches_the_waiting_session(factory, tmp_path, until):
    task_id = await _seed(factory, tmp_path)
    user = FakeUser(id=str(_USER), username="tester")
    waiter = factory()
    async with waiter as session:
        repo = Repo(session)
        task = await repo.get_task_for_user(_USER, task_id)
        recording = await repo.recording_for_task(task_id)
        assert not tools_mod._wait_condition_met(task, until, recording)

        # A worker commits from another session.
        async with factory() as other:
            orepo = Repo(other)
            ot = await orepo.get_task_for_user(_USER, task_id)
            orec = await orepo.recording_for_task(task_id)
            orec.summary_path = str(tmp_path / "summary.md")
            ot.status = TaskStatus.completed
            await other.commit()

        # The next check, in the waiting session, must see it.
        tools_mod._forget_loaded_rows(repo)
        task = await repo.get_task_for_user(_USER, task_id)
        recording = await repo.recording_for_task(task_id)
        assert tools_mod._wait_condition_met(task, until, recording)


async def test_wait_for_task_notices_a_commit_made_while_waiting(factory, tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(tools_mod, "_WAIT_POLL_INTERVAL_SECONDS", 0.05)
    task_id = await _seed(factory, tmp_path)
    user = FakeUser(id=str(_USER), username="tester")

    async def worker():
        await asyncio.sleep(0.15)
        async with factory() as other:
            rec = await Repo(other).recording_for_task(task_id)
            rec.summary_path = str(tmp_path / "summary.md")
            await other.commit()

    asyncio.create_task(worker())
    async with factory() as session:
        res = await tools_mod.wait_for_task(
            task_id=task_id, until="summary", timeout_seconds=3, user=user,
            repo=Repo(session), redis=FakeRedisWithPubSub(), events_channel="vts:events",
        )
    assert res.reached is True

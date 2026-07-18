import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
from vts.db.repo import Repo

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


def _mk_task(session, task_id):
    session.add(
        Task(
            id=task_id, user_id=_USER, source_url="x", artifact_dir="/tmp/x",
            options={}, status=TaskStatus.completed,
        )
    )


@pytest.mark.asyncio
async def test_speaker_names_for_task(factory):
    task_id = uuid.uuid4()
    async with factory() as s:
        repo = Repo(s)
        _mk_task(s, task_id)
        vasya = await repo.create_speaker(_USER, "Вася")
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=vasya.id, voice_sample_id=None, distance=0.1,
            embedding_model="m", outcome="confirmed",
        )
        # a left-anonymous decision (speaker_id None) must NOT appear
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_01",
            speaker_id=None, voice_sample_id=None, distance=None,
            embedding_model="m", outcome="left_anonymous",
        )
        await s.commit()

        assert await repo.speaker_names_for_task(_USER, task_id) == {"SPEAKER_00": "Вася"}


@pytest.mark.asyncio
async def test_speaker_names_for_task_drops_deleted_speaker(factory):
    """A deleted person leaves the decision behind with speaker_id NULL; the
    label must fall out of the map so rendering falls back to 'Голос N'."""
    task_id = uuid.uuid4()
    async with factory() as s:
        repo = Repo(s)
        _mk_task(s, task_id)
        sp = await repo.create_speaker(_USER, "Удалённый")
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=sp.id, voice_sample_id=None, distance=0.1,
            embedding_model="m", outcome="confirmed",
        )
        await s.commit()
        await repo.delete_speaker(_USER, sp.id)
        await s.commit()

        assert await repo.speaker_names_for_task(_USER, task_id) == {}


@pytest.mark.asyncio
async def test_speaker_names_for_task_latest_decision_wins(factory):
    """Rebinding a label within the dialog records a second decision; the most
    recent one must win."""
    task_id = uuid.uuid4()
    async with factory() as s:
        repo = Repo(s)
        _mk_task(s, task_id)
        first = await repo.create_speaker(_USER, "Первый")
        second = await repo.create_speaker(_USER, "Второй")
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=first.id, voice_sample_id=None, distance=0.2,
            embedding_model="m", outcome="confirmed",
        )
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=second.id, voice_sample_id=None, distance=0.1,
            embedding_model="m", outcome="confirmed",
        )
        await s.commit()

        assert await repo.speaker_names_for_task(_USER, task_id) == {"SPEAKER_00": "Второй"}


@pytest.mark.asyncio
async def test_speaker_names_for_task_isolation(factory):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    task_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=other, username="other"))
        repo = Repo(s)
        _mk_task(s, task_id)
        vasya = await repo.create_speaker(_USER, "Вася")
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=vasya.id, voice_sample_id=None, distance=0.1,
            embedding_model="m", outcome="confirmed",
        )
        await s.commit()

        assert await repo.speaker_names_for_task(other, task_id) == {}

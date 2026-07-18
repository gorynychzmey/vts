import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import MatchDecision, User
from vts.db.repo import Repo

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


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
        s.add(User(id=_OTHER, username="other"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _mk_sample(repo, speaker_id, model="m1"):
    return await repo.add_voice_sample(
        speaker_id=speaker_id,
        embedding=[0.1] * 256,
        embedding_model=model,
        audio=b"x",
        audio_format="wav",
        duration_sec=5.0,
        source_task_id=None,
    )


@pytest.mark.asyncio
async def test_move_voice_sample_changes_speaker_not_decision(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        vs = await _mk_sample(repo, a.id)
        await repo.record_decision(
            user_id=_USER, source_task_id=None, speaker_label="S0",
            speaker_id=a.id, voice_sample_id=vs.id, distance=0.1,
            embedding_model="m1", outcome="confirmed",
        )
        await s.commit()

        moved = await repo.move_voice_sample(_USER, vs.id, b.id)
        await s.commit()
        assert moved is not None and moved.speaker_id == b.id
        # move must NOT touch calibration history — the decision still points at A
        dec = (await s.scalars(select(MatchDecision))).one()
        assert dec.speaker_id == a.id


@pytest.mark.asyncio
async def test_move_voice_sample_isolation(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        vs = await _mk_sample(repo, a.id)
        await s.commit()
        # another user must not be able to move this user's sample
        assert await repo.move_voice_sample(_OTHER, vs.id, b.id) is None


@pytest.mark.asyncio
async def test_move_voice_sample_missing_target(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        vs = await _mk_sample(repo, a.id)
        await s.commit()
        assert await repo.move_voice_sample(_USER, vs.id, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_reassign_speaker_samples_moves_all(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        await _mk_sample(repo, a.id)
        await _mk_sample(repo, a.id)
        await s.commit()

        assert await repo.reassign_speaker_samples(_USER, a.id, b.id) == 2
        await s.commit()
        assert await repo.list_voice_samples(a.id) == []
        assert len(await repo.list_voice_samples(b.id)) == 2


@pytest.mark.asyncio
async def test_reassign_speaker_samples_isolation(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        await _mk_sample(repo, a.id)
        await s.commit()

        assert await repo.reassign_speaker_samples(_OTHER, a.id, b.id) == 0
        await s.commit()
        assert len(await repo.list_voice_samples(a.id)) == 1


@pytest.mark.asyncio
async def test_merge_moves_samples_rewrites_decisions_deletes_source(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "Вася-1")
        b = await repo.create_speaker(_USER, "Вася-2")
        a_id, b_id = a.id, b.id
        vs_a = await _mk_sample(repo, a_id)
        await _mk_sample(repo, b_id)
        await repo.record_decision(
            user_id=_USER, source_task_id=None, speaker_label="S0",
            speaker_id=a_id, voice_sample_id=vs_a.id, distance=0.1,
            embedding_model="m1", outcome="confirmed",
        )
        await s.commit()

        assert await repo.merge_speakers(_USER, a_id, b_id) is True
        await s.commit()

    async with factory() as s:
        repo = Repo(s)
        # source gone
        assert await repo.get_speaker(_USER, a_id) is None
        # both fragments now live under the target
        samples = await repo.list_voice_samples(b_id)
        assert len(samples) == 2
        assert {v.speaker_id for v in samples} == {b_id}
        # decision rewritten a -> b, so the name survives in old tasks
        dec = (await s.scalars(select(MatchDecision))).one()
        assert dec.speaker_id == b_id


@pytest.mark.asyncio
async def test_merge_same_speaker_false(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        await s.commit()
        assert await repo.merge_speakers(_USER, a.id, a.id) is False


@pytest.mark.asyncio
async def test_merge_isolation(factory):
    async with factory() as s:
        repo = Repo(s)
        a = await repo.create_speaker(_USER, "A")
        b = await repo.create_speaker(_USER, "B")
        await s.commit()

        assert await repo.merge_speakers(_OTHER, a.id, b.id) is False
        await s.commit()
        # both survive — no cross-user deletion
        assert await repo.get_speaker(_USER, a.id) is not None
        assert await repo.get_speaker(_USER, b.id) is not None

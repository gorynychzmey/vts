import pytest
from vts.services.speaker_registry import bucket, MatchOutcome


@pytest.mark.parametrize("dist,expected", [
    (None, MatchOutcome.miss),
    (0.10, MatchOutcome.auto),
    (0.30, MatchOutcome.auto),   # == auto boundary is auto
    (0.31, MatchOutcome.grey),
    (0.60, MatchOutcome.grey),   # == candidate boundary is grey
    (0.61, MatchOutcome.miss),
])
def test_bucket(dist, expected):
    assert bucket(dist, auto=0.30, candidate=0.60) == expected


import uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo
from _db import make_test_engine, ensure_pgvector

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


@pytest.mark.asyncio
async def test_nearest_by_min_distance_and_model_filter(factory):
    async with factory() as s:
        repo = Repo(s)
        vasya = await repo.create_speaker(_USER, "Вася")
        petya = await repo.create_speaker(_USER, "Петя")
        # Vasya has two samples; the nearer one must decide his rank (MIN).
        await repo.add_voice_sample(speaker_id=vasya.id, embedding=[1.0] + [0.0]*255,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await repo.add_voice_sample(speaker_id=vasya.id, embedding=[0.9, 0.1] + [0.0]*254,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await repo.add_voice_sample(speaker_id=petya.id, embedding=[0.0, 1.0] + [0.0]*254,
            embedding_model="m1", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        # A sample from a different model must be excluded.
        await repo.add_voice_sample(speaker_id=petya.id, embedding=[1.0] + [0.0]*255,
            embedding_model="OTHER", audio=b"x", audio_format="wav", duration_sec=5, source_task_id=None)
        await s.commit()

        ranked = await repo.nearest_speakers(_USER, [1.0] + [0.0]*255, "m1")
        # Vasya first (has an identical-direction sample), Petya second.
        assert [sp.name for sp, _ in ranked] == ["Вася", "Петя"]
        # Petya's distance must come from his m1 sample, not the OTHER one.
        petya_dist = next(d for sp, d in ranked if sp.name == "Петя")
        assert petya_dist > 0.5

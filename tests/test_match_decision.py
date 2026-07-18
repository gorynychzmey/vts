import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vts.db.base import Base
from vts.db.models import User, MatchDecision
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
async def test_override_writes_two_rows(factory):
    async with factory() as s:
        repo = Repo(s)
        vasya = await repo.create_speaker(_USER, "Вася")
        petya = await repo.create_speaker(_USER, "Петя")
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S2",
            speaker_id=vasya.id, voice_sample_id=None, distance=0.67,
            embedding_model="m1", outcome="rejected")
        await repo.record_decision(user_id=_USER, source_task_id=None, speaker_label="S2",
            speaker_id=petya.id, voice_sample_id=None, distance=0.65,
            embedding_model="m1", outcome="confirmed")
        await s.commit()
        rows = (await s.scalars(select(MatchDecision).order_by(MatchDecision.distance))).all()
        assert [(r.outcome, r.distance) for r in rows] == [("confirmed", 0.65), ("rejected", 0.67)]

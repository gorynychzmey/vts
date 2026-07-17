import uuid
import pytest
from sqlalchemy import select

from vts.db.models import Speaker, VoiceSample
from tests._db import make_test_engine, ensure_pgvector
from vts.db.base import Base
from vts.db.models import User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
async def test_speaker_and_sample_roundtrip(factory):
    async with factory() as s:
        sp = Speaker(user_id=_USER, name="Вася")
        s.add(sp)
        await s.flush()
        vs = VoiceSample(
            speaker_id=sp.id,
            embedding=[0.1] * 256,
            embedding_model="wespeaker-resnet34-256",
            audio=b"RIFF....",
            audio_format="wav",
            duration_sec=5.0,
            source_task_id=None,
        )
        s.add(vs)
        await s.commit()
    async with factory() as s:
        got = await s.scalar(select(VoiceSample).where(VoiceSample.speaker_id == sp.id))
        assert got is not None
        assert len(got.embedding) == 256
        assert got.embedding_model == "wespeaker-resnet34-256"

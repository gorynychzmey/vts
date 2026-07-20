import uuid
import pytest
from sqlalchemy import select

from vts.db.models import Speaker, VoiceSample
from tests._db import make_test_engine, ensure_pgvector
from vts.db.base import Base
from vts.db.models import Task, TaskStatus, User
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


from vts.db.repo import Repo


@pytest.mark.asyncio
async def test_speaker_crud_and_isolation(factory):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    async with factory() as s:
        s.add(User(id=other, username="other"))
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        await s.commit()
        assert (await repo.get_speaker(other, sp.id)) is None  # isolation
        rows = await repo.list_speakers(_USER)
        assert [r.name for r in rows] == ["Вася"]
        renamed = await repo.rename_speaker(_USER, sp.id, "Василий")
        assert renamed.name == "Василий"
        assert await repo.delete_speaker(_USER, sp.id) is True
        assert await repo.list_speakers(_USER) == []


@pytest.mark.asyncio
async def test_delete_speaker_cascades_samples(factory):
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256,
            embedding_model="m", audio=b"x", audio_format="wav",
            duration_sec=5.0, source_task_id=None,
        )
        await s.commit()
        assert len(await repo.list_voice_samples(sp.id)) == 1
        await repo.delete_speaker(_USER, sp.id)
        await s.commit()
    async with factory() as s:
        repo = Repo(s)
        assert await repo.list_voice_samples(sp.id) == []


@pytest.mark.asyncio
async def test_load_sample_audio_and_delete(factory):
    async with factory() as s:
        repo = Repo(s)
        sp = await repo.create_speaker(_USER, "Вася")
        vs = await repo.add_voice_sample(
            speaker_id=sp.id, embedding=[0.1] * 256,
            embedding_model="m", audio=b"AUDIOBYTES", audio_format="wav",
            duration_sec=5.0, source_task_id=None,
        )
        await s.commit()
        audio, fmt = await repo.load_sample_audio(_USER, vs.id)
        assert audio == b"AUDIOBYTES" and fmt == "wav"
        assert await repo.delete_voice_sample(_USER, vs.id) is True
        assert await repo.load_sample_audio(_USER, vs.id) is None


@pytest.mark.asyncio
async def test_record_decision_persists_is_noise(factory):
    from vts.db.repo import Repo
    async with factory() as s:
        repo = Repo(s)
        row = await repo.record_decision(
            user_id=_USER,
            source_task_id=None,
            speaker_label="SPEAKER_01",
            speaker_id=None,
            voice_sample_id=None,
            distance=None,
            embedding_model="m",
            outcome="left_anonymous",
            is_noise=True,
        )
        assert row.is_noise is True


@pytest.mark.asyncio
async def test_noise_labels_from_decisions(factory):
    from vts.db.repo import Repo
    task_id = uuid.uuid4()
    async with factory() as s:
        s.add(
            Task(
                id=task_id, user_id=_USER, source_url="x", artifact_dir="/tmp/x",
                options={}, status=TaskStatus.completed,
            )
        )
        await s.flush()
        repo = Repo(s)
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_00",
            speaker_id=None, voice_sample_id=None, distance=None,
            embedding_model="m", outcome="left_anonymous", is_noise=False,
        )
        await repo.record_decision(
            user_id=_USER, source_task_id=task_id, speaker_label="SPEAKER_01",
            speaker_id=None, voice_sample_id=None, distance=None,
            embedding_model="m", outcome="left_anonymous", is_noise=True,
        )
        await s.commit()
        labels = await repo.noise_labels_from_decisions(_USER, task_id)
        assert labels == {"SPEAKER_01"}


@pytest.mark.asyncio
async def test_noise_labels_empty_when_no_decisions(factory):
    from vts.db.repo import Repo
    async with factory() as s:
        repo = Repo(s)
        labels = await repo.noise_labels_from_decisions(_USER, uuid.uuid4())
        assert labels == set()

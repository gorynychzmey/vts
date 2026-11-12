"""Player blocks addressed by RECORDING (vts-vtma.1).

The MCP Apps player shows a recording — the lasting object — not a task, so it
needs the same blocks the /player page gets from a task: sentences with their
timecodes, and a resolved speaker label instead of the raw SPEAKER_NN tag.

Voice identification belongs to the task that produced the recording. While
that task exists its names are used; once it is deleted the decisions no longer
point anywhere, and the labels fall back to the plain speaker word — the same
rule `RecordingInfo.people` follows.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.api._helpers.artifact_store import (
    _load_player_blocks,
    load_recording_player_blocks,
)
from vts.db.base import Base
from vts.db.models import Speaker, User
from vts.db.repo import Repo

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000b7")


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


def _seed(root):
    outputs = root / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "transcript.txt").write_text("Привет. Ответ.", encoding="utf-8")
    (outputs / "transcript.json").write_text(
        json.dumps({"entries": [
            {"start": 0.0, "end": 2.0, "text": "Привет.", "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 5.0, "text": "Ответ.", "speaker": "SPEAKER_01"},
        ]}),
        encoding="utf-8",
    )


async def _recording_with_named_voice(session, tmp_path, *, language="ru"):
    """A recording whose task bound SPEAKER_00 to «Вася»; SPEAKER_01 unnamed."""
    _seed(tmp_path)
    repo = Repo(session)
    task = await repo.create_task(
        user_id=_USER, source_url="file://clip.m4a", options={"language": language},
        artifact_dir=str(tmp_path),
    )
    task.transcript_path = str(tmp_path / "outputs" / "transcript.txt")
    person = Speaker(user_id=_USER, name="Вася")
    session.add(person)
    await session.flush()
    await repo.record_decision(
        user_id=_USER, source_task_id=task.id, speaker_label="SPEAKER_00",
        speaker_id=person.id, voice_sample_id=None, distance=None,
        embedding_model="m", outcome="manual_match", is_noise=False,
    )
    recording = await repo.upsert_recording_for_task(task)
    recording.language = language
    await session.commit()
    return task, recording


def _labels(blocks):
    return [(b["label"], [s["text"] for s in b["sentences"]]) for b in blocks]


@pytest.mark.asyncio
async def test_a_recording_gets_the_same_blocks_as_its_task(factory, tmp_path):
    async with factory() as session:
        task, recording = await _recording_with_named_voice(session, tmp_path)
        by_task = await _load_player_blocks(task, session)
        by_recording = await load_recording_player_blocks(recording, session)
    assert by_recording == by_task
    assert _labels(by_recording) == [("Вася", ["Привет."]), ("Голос 2", ["Ответ."])]


@pytest.mark.asyncio
async def test_after_the_task_is_deleted_the_labels_fall_back(factory, tmp_path):
    async with factory() as session:
        task, recording = await _recording_with_named_voice(session, tmp_path)
        await session.delete(task)
        recording.source_task_id = None
        await session.commit()
        blocks = await load_recording_player_blocks(recording, session)
    # The transcript is still there; only the names, which belonged to the
    # task, are gone. Never the raw tag.
    assert _labels(blocks) == [("Голос 1", ["Привет."]), ("Голос 2", ["Ответ."])]


@pytest.mark.asyncio
async def test_the_label_word_follows_the_recording_language(factory, tmp_path):
    async with factory() as session:
        task, recording = await _recording_with_named_voice(session, tmp_path, language="en")
        await session.delete(task)
        recording.source_task_id = None
        await session.commit()
        blocks = await load_recording_player_blocks(recording, session)
    assert [b["label"] for b in blocks] == ["Speaker 1", "Speaker 2"]


@pytest.mark.asyncio
async def test_a_recording_without_a_transcript_has_no_blocks(factory, tmp_path):
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="file://clip.m4a", options={},
            artifact_dir=str(tmp_path),
        )
        recording = await repo.upsert_recording_for_task(task)
        await session.commit()
        assert await load_recording_player_blocks(recording, session) == []

"""Storing the decomposed axes alongside (and eventually instead of) raw_json.

The pipeline writes both forms during the transition (vts-6qwy): `payload`
carries the decomposed axes, `raw_json` stays until a separate, irreversible
step clears it. Every consumer reads through `segment_raw_payload`, which
prefers the axes — so clearing the legacy column needs no coordinated deploy.

This is the test that makes that claim checkable rather than hopeful: it writes
through the real repo path, clears raw_json the way the cleanup will, and
asserts both consumers see exactly what they saw before.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import User
from vts.db.repo import Repo
from vts.services.asr_payload import segment_raw_payload
from vts.services.diarization.merge import usable_words
from vts.services.player_transcript import _shifted_inner_sentences

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_RAW = {
    "text": " Привет мир",
    "language": "ru",
    "duration": 5.0,
    "segments": [
        {
            "id": 0, "start": 0.0, "end": 2.5, "text": " Привет",
            "tokens": [50364, 1234], "temperature": 0.0,
            "avg_logprob": -0.31, "no_speech_prob": 0.02,
            "words": [
                {"word": " При", "start": 0.0, "end": 1.0, "probability": 0.9, "t_dtw": -1},
                {"word": "вет", "start": 1.0, "end": 2.5, "probability": 0.8, "t_dtw": -1},
            ],
        },
        {
            "id": 1, "start": 2.5, "end": 5.0, "text": " мир",
            "tokens": [5678], "temperature": 0.0,
            "avg_logprob": -0.22, "no_speech_prob": 0.01,
            "words": [{"word": " мир", "start": 2.5, "end": 5.0, "probability": 0.95}],
        },
    ],
}


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


def _words(segment):
    return [
        (w.get("word"), w.get("start"), w.get("end"), w.get("probability"))
        for w in (usable_words(segment_raw_payload(segment)) or [])
    ]


def _sentences(segment):
    return _shifted_inner_sentences([{"start": 0.0, "raw_json": segment_raw_payload(segment)}])


async def _seed(factory):
    async with factory() as session:
        repo = Repo(session)
        task = await repo.create_task(
            user_id=_USER, source_url="https://example.com/v",
            options={}, artifact_dir="/tmp/x",
        )
        await repo.upsert_asr_segment_payload(
            task_id=task.id, segment_index=0, start_sec=0.0, end_sec=5.0,
            text="Привет мир", raw_json=_RAW,
        )
        await session.commit()
        return task.id


@pytest.mark.asyncio
async def test_writing_a_segment_derives_the_axes(factory):
    task_id = await _seed(factory)
    async with factory() as session:
        segments = await Repo(session).get_task_segments(task_id)
        assert segments[0].payload, "the repo did not derive the axes on write"
        assert segments[0].payload["tokens"]
        assert segments[0].payload["sentences"]


@pytest.mark.asyncio
async def test_consumers_are_unaffected_by_clearing_the_legacy_column(factory):
    task_id = await _seed(factory)
    async with factory() as session:
        segment = (await Repo(session).get_task_segments(task_id))[0]
        before_words, before_sentences = _words(segment), _sentences(segment)
        # Sanity: the fixture really exercises subword gluing.
        assert [w[0] for w in before_words] == ["Привет", "мир"]

    async with factory() as session:
        await session.execute(text("UPDATE asr_segments SET raw_json = '{}'::json"))
        await session.commit()

    # A fresh session, so the cleared column is re-read rather than served from
    # the identity map of the session that wrote it.
    async with factory() as session:
        segment = (await Repo(session).get_task_segments(task_id))[0]
        assert _words(segment) == before_words, "word axis changed once raw_json was cleared"
        assert _sentences(segment) == before_sentences, "sentence axis changed once raw_json was cleared"


@pytest.mark.asyncio
async def test_a_legacy_row_without_axes_still_reads(factory):
    # Rows written before the migration carry raw_json only; they must keep
    # working until the backfill reaches them.
    task_id = await _seed(factory)
    async with factory() as session:
        await session.execute(text("UPDATE asr_segments SET payload = NULL"))
        await session.commit()
    async with factory() as session:
        segment = (await Repo(session).get_task_segments(task_id))[0]
        assert [w[0] for w in _words(segment)] == ["Привет", "мир"]
        assert len(_sentences(segment)) == 2

"""Corpus search with a relevance threshold (vts-uurt / VOS-132).

The requirement that shapes everything: **below the threshold, return nothing**.
Not the k nearest rows — nothing. The reason is written into the task: a vector
store without a threshold answers every query with its top-k, so a question the
corpus cannot answer comes back with confident, irrelevant passages. That is the
upstream Cognee behaviour this is explicitly built not to repeat.

The threshold was calibrated against the real deployment before this was
written, not guessed. Embedding the corpus and querying it:

    answerable queries    scored 0.521 .. 0.762
    unanswerable queries  scored 0.317 .. 0.379

so there is a clean separating band, and the default sits inside it.

The second constraint is that a threshold is a POST-filter over an ANN result:
the index returns k rows and the cut happens after. If k is too small the
selection size decides the answer rather than the threshold, which is the very
behaviour being avoided — so the search over-fetches and then cuts.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import TranscriptChunk, User
from vts.db.repo import Repo
from vts.services.corpus_search import DEFAULT_THRESHOLD, search_chunks

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_DIMS = 1024


def _vec(*, near: float) -> list[float]:
    """A unit vector whose cosine similarity to _query() is `near`.

    Two dimensions carry the signal; the rest are zero. Keeps the fixtures
    exact rather than approximately similar.
    """
    import math

    return [near, math.sqrt(max(0.0, 1.0 - near * near))] + [0.0] * (_DIMS - 2)


def _query() -> list[float]:
    return [1.0, 0.0] + [0.0] * (_DIMS - 2)


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
        s.add(User(id=_OTHER, username="someone-else"))
        await s.commit()
    yield f
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed(session, similarities, *, user_id=_USER, title="Team sync"):
    repo = Repo(session)
    task = await repo.create_task(
        user_id=user_id, source_url="https://example.com/v",
        options={}, artifact_dir="/tmp/x",
    )
    task.source_title = title
    recording = await repo.upsert_recording_for_task(task)
    for index, similarity in enumerate(similarities):
        session.add(TranscriptChunk(
            recording_id=recording.id, user_id=user_id, chunk_index=index,
            text=f"passage {index} at similarity {similarity}",
            start_sec=index * 30.0, end_sec=index * 30.0 + 30.0,
            speakers=["SPEAKER_00"], embedding=_vec(near=similarity),
            embedding_model="bge-m3",
        ))
    await session.commit()
    return recording


@pytest.mark.asyncio
async def test_returns_matches_above_the_threshold_best_first(factory):
    async with factory() as session:
        await _seed(session, [0.90, 0.70, 0.55])
        hits = await search_chunks(session, _USER, _query(), threshold=0.5)
        assert [round(h.score, 2) for h in hits] == [0.90, 0.70, 0.55]
        assert hits[0].text.startswith("passage 0")


@pytest.mark.asyncio
async def test_a_corpus_with_nothing_relevant_returns_empty_not_top_k(factory):
    """The whole point of the feature.

    Every chunk here is far from the query. A plain ANN search would still hand
    back its three nearest rows, and a caller — human or LLM — would read them
    as answers.
    """
    async with factory() as session:
        await _seed(session, [0.31, 0.28, 0.20])
        hits = await search_chunks(session, _USER, _query(), threshold=DEFAULT_THRESHOLD)
        assert hits == [], f"returned {len(hits)} irrelevant passages instead of nothing"


@pytest.mark.asyncio
async def test_the_threshold_decides_the_cut_not_the_fetch_size(factory):
    """A threshold is a post-filter over an ANN result, so k must not decide.

    With 12 chunks above the threshold and a limit of 3, the caller asked for 3
    — but the ones returned must be the 3 BEST, not whatever the index happened
    to surface first.
    """
    async with factory() as session:
        await _seed(session, [0.55 + i * 0.01 for i in range(12)])
        hits = await search_chunks(session, _USER, _query(), threshold=0.5, limit=3)
        assert len(hits) == 3
        assert [round(h.score, 2) for h in hits] == [0.66, 0.65, 0.64]


@pytest.mark.asyncio
async def test_a_higher_threshold_narrows_the_result(factory):
    async with factory() as session:
        await _seed(session, [0.90, 0.70, 0.55])
        assert len(await search_chunks(session, _USER, _query(), threshold=0.5)) == 3
        assert len(await search_chunks(session, _USER, _query(), threshold=0.8)) == 1
        assert await search_chunks(session, _USER, _query(), threshold=0.95) == []


@pytest.mark.asyncio
async def test_search_is_scoped_to_the_user(factory):
    # Not an optimisation: another user's transcripts must be invisible, and a
    # search endpoint is a new way to ask for them.
    async with factory() as session:
        await _seed(session, [0.95], user_id=_OTHER, title="Someone else's call")
        assert await search_chunks(session, _USER, _query(), threshold=0.5) == []


@pytest.mark.asyncio
async def test_each_hit_carries_what_a_citation_needs(factory):
    async with factory() as session:
        recording = await _seed(session, [0.90])
        hit = (await search_chunks(session, _USER, _query(), threshold=0.5))[0]
        # The stable identifier is the RECORDING, not the task: the task can be
        # deleted, the recording is what lasts.
        assert hit.recording_id == recording.id
        assert hit.title == "Team sync"
        assert hit.start_sec == 0.0 and hit.end_sec == 30.0
        assert hit.speakers == ["SPEAKER_00"]
        assert 0.0 <= hit.score <= 1.0


@pytest.mark.asyncio
async def test_chunks_without_an_embedding_are_skipped(factory):
    # A chunk exists as soon as its text is split; the vector arrives later.
    async with factory() as session:
        recording = await _seed(session, [0.90])
        session.add(TranscriptChunk(
            recording_id=recording.id, user_id=_USER, chunk_index=99,
            text="not embedded yet", start_sec=0.0, end_sec=1.0,
            speakers=[], embedding=None, embedding_model=None,
        ))
        await session.commit()
        hits = await search_chunks(session, _USER, _query(), threshold=0.5)
        assert all(h.text != "not embedded yet" for h in hits)


@pytest.mark.asyncio
async def test_an_empty_corpus_is_a_miss_not_an_error(factory):
    async with factory() as session:
        assert await search_chunks(session, _USER, _query(), threshold=0.5) == []


@pytest.mark.asyncio
async def test_results_can_be_confined_to_one_recording(factory):
    async with factory() as session:
        first = await _seed(session, [0.90], title="First")
        await _seed(session, [0.92], title="Second")
        hits = await search_chunks(
            session, _USER, _query(), threshold=0.5, recording_id=first.id
        )
        assert [h.title for h in hits] == ["First"]


# ----------------------------------------------------------------- HTTP API

@pytest.mark.asyncio
async def test_search_endpoint_returns_hits_and_echoes_the_threshold(
    authed_app, client, monkeypatch
):
    from tests.conftest import _TEST_USER_ID
    from vts.services import corpus_search as cs

    _app, session_factory = authed_app
    async with session_factory() as session:
        await _seed(session, [0.90, 0.30], user_id=uuid.UUID(_TEST_USER_ID))

    # Stand in for the gateway: the query embeds to the same direction the
    # fixtures were built around.
    class _Stub:
        def __init__(self, **kw): pass
        async def embed(self, texts): return [_query() for _ in texts]

    monkeypatch.setattr(cs, "EmbeddingClient", _Stub, raising=False)
    monkeypatch.setattr("vts.services.embeddings.EmbeddingClient", _Stub)

    r = await client.get("/api/search?q=что%20обсуждали")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["threshold"] == pytest.approx(DEFAULT_THRESHOLD)
    # Only the passage above the threshold; the 0.30 one is not "the next best".
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["score"] > DEFAULT_THRESHOLD
    assert hit["recording_id"]
    assert hit["start_sec"] == 0.0


@pytest.mark.asyncio
async def test_search_endpoint_answers_empty_rather_than_nearest(
    authed_app, client, monkeypatch
):
    from tests.conftest import _TEST_USER_ID
    from vts.services import corpus_search as cs

    _app, session_factory = authed_app
    async with session_factory() as session:
        await _seed(session, [0.31, 0.25], user_id=uuid.UUID(_TEST_USER_ID))

    class _Stub:
        def __init__(self, **kw): pass
        async def embed(self, texts): return [_query() for _ in texts]

    monkeypatch.setattr("vts.services.embeddings.EmbeddingClient", _Stub)

    r = await client.get("/api/search?q=про%20что-то%20другое")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["hits"] == []
    # The threshold comes back so the caller can tell "nothing is this
    # relevant" from "the corpus is empty".
    assert payload["threshold"] > 0


@pytest.mark.asyncio
async def test_search_endpoint_does_not_reach_another_users_corpus(
    authed_app, client, monkeypatch
):
    from vts.db.models import User
    from vts.services import corpus_search as cs

    _app, session_factory = authed_app
    other = uuid.uuid4()
    async with session_factory() as session:
        session.add(User(id=other, username="someone-else"))
        await session.flush()
        await _seed(session, [0.99], user_id=other, title="Private call")

    class _Stub:
        def __init__(self, **kw): pass
        async def embed(self, texts): return [_query() for _ in texts]

    monkeypatch.setattr("vts.services.embeddings.EmbeddingClient", _Stub)

    r = await client.get("/api/search?q=anything")
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == [], "another user's transcript was searchable"


@pytest.mark.asyncio
async def test_an_empty_query_searches_nothing(authed_app, client):
    r = await client.get("/api/search?q=%20%20")
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []

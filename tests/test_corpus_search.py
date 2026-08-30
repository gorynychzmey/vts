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


# --------------------------------------------------------- cross-language

@pytest.mark.asyncio
async def test_the_threshold_is_language_agnostic(factory):
    """Retrieval must not depend on the query's language.

    bge-m3 is multilingual, and this was verified against the real Russian
    corpus: four English questions returned the SAME chunk ids as their Russian
    equivalents, scoring 0.556/0.556, 0.537/0.505, 0.595/0.554 and 0.495/0.493
    — English consistently a little lower, but comfortably above the threshold.
    Controls ("borscht recipe", "quantum chromodynamics") stayed empty in both
    languages.

    The margin matters and is why this test exists: English scores land 0.00 to
    0.04 below their Russian counterparts, so a threshold raised much past the
    calibrated 0.45 would start dropping cross-language hits while Russian ones
    still passed — a failure mode that shows up only for non-Russian users and
    would otherwise be invisible here.
    """
    async with factory() as session:
        # 0.49 stands for the weakest measured cross-language hit (0.493).
        await _seed(session, [0.49])
        assert len(await search_chunks(session, _USER, _query(), threshold=DEFAULT_THRESHOLD)) == 1, (
            "the default threshold rejects a passage that a real English query "
            "matched at this score"
        )
        # Documented headroom: the calibrated default leaves room for the gap.
        assert DEFAULT_THRESHOLD <= 0.49


# ------------------------------------------------------ the threshold is config

def test_the_threshold_comes_from_config_yaml():
    """`services.search_threshold` in config.yaml must reach the search.

    The default is the calibrated 0.45, but the value has to be an operator's
    to change — this deployment's corpus is not every corpus, and the honest
    way to move the bar is a setting rather than an edit. Asserted end to end
    (yaml -> Settings) because a mapping typo would leave the knob silently
    inert while the default kept working.
    """
    import yaml

    from vts.core.config import Settings, _normalize_yaml_overrides

    # The yaml READER is stubbed out for the whole test run (tests/conftest.py
    # blanks _load_yaml_overrides so the host's production config cannot leak
    # in), so this drives the normaliser directly and builds Settings from what
    # it produces — the same two steps get_settings performs. Verified against
    # the real reader outside pytest: `services.search_threshold: 0.72` in a
    # config.yaml yields Settings().search_threshold == 0.72.
    document = yaml.safe_load("services:\n  search_threshold: 0.72\n")
    overrides = _normalize_yaml_overrides(document)
    assert overrides.get("search_threshold") == pytest.approx(0.72), (
        f"services.search_threshold is not mapped onto the setting: {overrides}"
    )
    assert Settings(**overrides).search_threshold == pytest.approx(0.72)


def test_the_default_is_the_calibrated_value():
    # Not a round number chosen for looks: the midpoint of the measured
    # separating band (0.379..0.521), kept low enough for cross-language hits.
    from vts.core.config import Settings

    assert Settings().search_threshold == pytest.approx(DEFAULT_THRESHOLD)
    assert 0.379 < DEFAULT_THRESHOLD < 0.493


@pytest.mark.asyncio
async def test_a_request_may_override_the_configured_threshold(factory):
    # A caller that would rather see weak matches than nothing can ask; the
    # default stays strict.
    async with factory() as session:
        await _seed(session, [0.40])
        assert await search_chunks(session, _USER, _query(), threshold=DEFAULT_THRESHOLD) == []
        assert len(await search_chunks(session, _USER, _query(), threshold=0.35)) == 1


# ------------------------------------------------- following a hit to the audio

@pytest.mark.asyncio
async def test_a_hit_carries_enough_to_build_a_deep_link(factory):
    """A citation has to be followable, and the player is addressed by TASK.

    Search returns `recording_id` as the stable identifier — correctly, since
    the recording outlives its task. But /player/{task_id} is what exists, so a
    result that carried only the recording id could not be linked to at all.

    So a hit also carries `source_task_id` (null once the task is gone) plus
    the start time, which is what `?t=` needs. When it IS null the citation is
    still valid as evidence — the passage and its timecode are real — it simply
    cannot be opened in a player any more, and the caller can see that rather
    than building a broken URL.
    """
    async with factory() as session:
        recording = await _seed(session, [0.90])
        hit = (await search_chunks(session, _USER, _query(), threshold=0.5))[0]
        assert hit.source_task_id == recording.source_task_id
        assert hit.source_task_id is not None
        # Everything /player/{task}?t= needs.
        assert hit.start_sec == 0.0


@pytest.mark.asyncio
async def test_a_hit_from_a_deleted_task_says_so_rather_than_lying(factory):
    async with factory() as session:
        recording = await _seed(session, [0.90])
        recording.source_task_id = None
        await session.commit()
        hit = (await search_chunks(session, _USER, _query(), threshold=0.5))[0]
        assert hit.source_task_id is None
        # The evidence itself is unaffected.
        assert hit.text and hit.recording_id == recording.id

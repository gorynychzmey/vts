"""Indexing a recording into searchable chunks (vts-twe7 / VOS-131).

Ties the three pieces together: split the transcript on speaker turns and time,
embed the pieces, store them against the RECORDING (which outlives the task).

The requirement that shapes the design is re-indexing: a transcript changes when
speakers are resolved or renamed (rerender_transcript), so indexing must be
repeatable and must not accumulate stale chunks. It replaces a recording's
chunks wholesale rather than diffing them — a diff would have to reason about
which chunk "is" which after the text moved, and getting that wrong leaves
passages that no longer exist in the transcript but still answer searches.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests._db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import TranscriptChunk, User
from vts.db.repo import Repo
from vts.services.indexing import index_recording

_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


class _FakeEmbedder:
    """Deterministic stand-in: one dimension per text, counting calls."""

    def __init__(self, dims: int = 1024, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._dims = dims
        self._fail = fail

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            from vts.services.embeddings import EmbeddingError
            raise EmbeddingError("gateway down")
        return [[float(len(t))] * self._dims for t in texts]


async def reindex_task_with(session, recording, entries, embedder):
    """`index_recording` behind the same failure isolation `reindex_task` uses.

    The production entry point builds its own embedding client from settings,
    so this mirrors it with an injectable one — the isolation being tested lives
    in `guarded_index`, which both go through.
    """
    from vts.services.indexing import guarded_index

    return await guarded_index(
        session, recording, entries, embedder=embedder, model_name="bge-m3"
    )


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


def _entries(n=6, speaker_alternating=True):
    out, t = [], 0.0
    for i in range(n):
        out.append({
            "start": t, "end": t + 12.0,
            "text": f"Реплика номер {i} про результаты работы и ближайшие планы команды. " * 3,
            "speaker": f"SPEAKER_0{i % 2}" if speaker_alternating else None,
        })
        t += 12.0
    return out


async def _recording(session):
    repo = Repo(session)
    task = await repo.create_task(
        user_id=_USER, source_url="https://example.com/v",
        options={}, artifact_dir="/tmp/x",
    )
    recording = await repo.upsert_recording_for_task(task)
    await session.commit()
    return recording


@pytest.mark.asyncio
async def test_indexing_stores_chunks_against_the_recording(factory):
    async with factory() as session:
        recording = await _recording(session)
        embedder = _FakeEmbedder()
        count = await index_recording(session, recording, _entries(), embedder=embedder,
                                      model_name="bge-m3")
        await session.commit()

        assert count > 0
        rows = (await session.execute(
            select(TranscriptChunk).where(TranscriptChunk.recording_id == recording.id)
            .order_by(TranscriptChunk.chunk_index)
        )).scalars().all()
        assert len(rows) == count
        assert [r.chunk_index for r in rows] == list(range(len(rows)))
        assert all(r.user_id == _USER for r in rows)
        assert all(r.embedding is not None for r in rows)
        assert all(r.embedding_model == "bge-m3" for r in rows)
        # Speaker tags are the technical ones, so a rename does not invalidate
        # the index.
        assert all(s.startswith("SPEAKER_") for r in rows for s in r.speakers)


@pytest.mark.asyncio
async def test_reindexing_replaces_rather_than_accumulates(factory):
    async with factory() as session:
        recording = await _recording(session)
        embedder = _FakeEmbedder()
        first = await index_recording(session, recording, _entries(8), embedder=embedder,
                                      model_name="bge-m3")
        await session.commit()

        # The transcript changed — as it does after speakers are resolved.
        second = await index_recording(session, recording, _entries(3), embedder=embedder,
                                       model_name="bge-m3")
        await session.commit()

        rows = (await session.execute(
            select(TranscriptChunk).where(TranscriptChunk.recording_id == recording.id)
        )).scalars().all()
        assert len(rows) == second, (
            f"re-indexing left {len(rows)} chunks for a {second}-chunk transcript "
            f"(first pass produced {first}) — stale passages still answer searches"
        )


@pytest.mark.asyncio
async def test_an_empty_transcript_clears_the_index(factory):
    # A recording whose transcript became empty must not keep answering with
    # what it used to say.
    async with factory() as session:
        recording = await _recording(session)
        await index_recording(session, recording, _entries(4), embedder=_FakeEmbedder(),
                              model_name="bge-m3")
        await session.commit()
        count = await index_recording(session, recording, [], embedder=_FakeEmbedder(),
                                      model_name="bge-m3")
        await session.commit()
        assert count == 0
        rows = (await session.execute(select(TranscriptChunk))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_a_failed_embedding_leaves_the_previous_index_intact(factory):
    """A gateway outage must cost a re-index, not the corpus.

    Asserted on the ORDER of operations, not just on the end state: a rollback
    would undo a premature delete too, so a test that only checks the rows
    afterwards passes even when the delete runs first. It is the ordering that
    protects a caller who commits between the two.
    """
    from vts.services.embeddings import EmbeddingError

    async with factory() as session:
        recording = await _recording(session)
        await index_recording(session, recording, _entries(4), embedder=_FakeEmbedder(),
                              model_name="bge-m3")
        await session.commit()
        before = (await session.execute(select(TranscriptChunk))).scalars().all()

        # Count the rows AT THE MOMENT the embedder is called: if the delete has
        # already happened, the index is gone before we know a new one exists.
        rows_when_embedding = {}

        class _Watching(_FakeEmbedder):
            async def embed(self, texts):
                result = await session.execute(select(TranscriptChunk))
                rows_when_embedding["count"] = len(result.scalars().all())
                raise EmbeddingError("gateway down")

        with pytest.raises(EmbeddingError):
            await index_recording(session, recording, _entries(6),
                                  embedder=_Watching(), model_name="bge-m3")

        assert rows_when_embedding.get("count") == len(before), (
            "the old index was deleted BEFORE the new embeddings were obtained — "
            "a gateway outage would leave the recording unsearchable"
        )

        await session.rollback()
        after = (await session.execute(select(TranscriptChunk))).scalars().all()
        assert len(after) == len(before), "a failed re-index destroyed the working index"


@pytest.mark.asyncio
async def test_deleting_the_recording_removes_its_chunks(factory):
    async with factory() as session:
        recording = await _recording(session)
        await index_recording(session, recording, _entries(4), embedder=_FakeEmbedder(),
                              model_name="bge-m3")
        await session.commit()
        await session.delete(recording)
        await session.commit()
        rows = (await session.execute(select(TranscriptChunk))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_an_undiarized_transcript_indexes_too(factory):
    # Diarization is optional and most transcripts lack it.
    async with factory() as session:
        recording = await _recording(session)
        count = await index_recording(
            session, recording, _entries(4, speaker_alternating=False),
            embedder=_FakeEmbedder(), model_name="bge-m3",
        )
        await session.commit()
        assert count > 0
        rows = (await session.execute(select(TranscriptChunk))).scalars().all()
        assert all(r.speakers == [] for r in rows)


@pytest.mark.asyncio
async def test_chunks_keep_their_place_in_the_recording(factory):
    # A citation is only useful if it can point back at the audio.
    async with factory() as session:
        recording = await _recording(session)
        await index_recording(session, recording, _entries(6), embedder=_FakeEmbedder(),
                              model_name="bge-m3")
        await session.commit()
        rows = (await session.execute(
            select(TranscriptChunk).order_by(TranscriptChunk.chunk_index)
        )).scalars().all()
        assert rows[0].start_sec == 0.0
        for a, b in zip(rows, rows[1:]):
            assert a.start_sec <= b.start_sec
            assert a.start_sec < a.end_sec


# ------------------------------------- isolation from the caller's transaction

@pytest.mark.asyncio
async def test_a_failed_index_does_not_poison_the_callers_transaction(factory):
    """Indexing failure must cost the index, never the user's own work.

    `reindex_task` catches everything and returns 0, which reads as "the caller
    is unaffected". It is not: a failure inside `flush()` leaves the SQLAlchemy
    session in a rolled-back state, so the caller's next `commit()` raises
    PendingRollbackError. At the resolve endpoint that session also holds the
    speaker decisions and the re-rendered transcript — so a broken embedding
    would answer 500 and silently discard a rename the user had just made,
    which is the exact outcome the docstring promises to prevent.

    Reproduced before fixing: inserting a wrong-dimension vector into
    HALFVEC(1024) raised DBAPIError on flush, and the caller's commit then
    failed with PendingRollbackError.
    """
    from vts.db.models import Recording

    async with factory() as session:
        recording = await _recording(session)

        class _BadVectors(_FakeEmbedder):
            async def embed(self, texts):
                # The realistic trigger: a configured model whose dimension does
                # not match the column.
                return [[0.5] * 7 for _ in texts]

        # The caller's own work, which must survive.
        recording.title = "renamed by the user"

        count = await reindex_task_with(session, recording, _entries(4), _BadVectors())
        assert count == 0, "a failing index reported success"

        # The point of the test: the caller can still commit.
        await session.commit()

    async with factory() as session:
        stored = (await session.execute(select(Recording))).scalars().first()
        assert stored.title == "renamed by the user", (
            "the user's change was lost because indexing failed"
        )

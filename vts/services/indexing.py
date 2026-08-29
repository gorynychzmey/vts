"""Indexing a recording into searchable chunks (vts-twe7 / VOS-131).

Ties three pieces together: split the transcript on speaker turns and time
(`chunking`), embed the pieces (`embeddings`), store them against the RECORDING
(which outlives the task, so the corpus does too).

**Re-indexing replaces, it does not diff.** A transcript changes whenever
speakers are resolved or renamed, and a diff would have to decide which stored
chunk "is" which after the text moved. Getting that wrong leaves passages that
no longer exist in the transcript but still answer searches — a silent, growing
wrongness. Deleting and re-inserting is cheap here (a recording is tens of
chunks) and cannot drift.

**Embed before deleting.** The old index is only removed once the new vectors
are in hand, so a gateway outage costs a re-index rather than the corpus: the
transaction rolls back with the working index still in place.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import TranscriptChunk
from vts.services.chunking import chunk_entries

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


async def index_recording(
    session: AsyncSession,
    recording: Any,
    entries: Any,
    *,
    embedder: Embedder,
    model_name: str,
) -> int:
    """(Re)build a recording's chunk index. Returns the number of chunks.

    The caller commits: indexing runs inside the transaction that produced the
    transcript, so a failure anywhere leaves neither a half-written index nor a
    task marked complete on the strength of one.
    """
    chunks = chunk_entries(entries)

    # Embed FIRST. If the gateway is down this raises before anything is
    # deleted, and the previous index survives the rollback.
    vectors: list[list[float]] = []
    if chunks:
        vectors = await embedder.embed([c["text"] for c in chunks])
        if len(vectors) != len(chunks):
            # The client already guards this; checking again here keeps the
            # mismatch from becoming a silent zip() truncation that pairs
            # vectors with the wrong passages.
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
            )

    await session.execute(
        delete(TranscriptChunk).where(TranscriptChunk.recording_id == recording.id)
    )
    # Flush the delete before inserting: the unique (recording_id, chunk_index)
    # would otherwise collide with the rows being replaced.
    await session.flush()

    for chunk, vector in zip(chunks, vectors):
        session.add(TranscriptChunk(
            recording_id=recording.id,
            user_id=recording.user_id,
            chunk_index=chunk["index"],
            text=chunk["text"],
            start_sec=chunk["start"],
            end_sec=chunk["end"],
            speakers=list(chunk["speakers"]),
            embedding=vector,
            embedding_model=model_name,
        ))
    await session.flush()
    return len(chunks)


async def reindex_task(session: AsyncSession, task: Any, settings: Any) -> int:
    """(Re)index the recording a task produced. Returns the chunk count.

    The entry point both triggers use: the end of a pipeline run, and
    `rerender_transcript` when resolving speakers changes the text. Keeping one
    function means the two paths cannot drift into indexing differently.

    Never raises. Indexing is derived data: a corpus that is briefly stale is a
    smaller problem than a completed transcript that reports failure, or a
    speaker rename that appears not to save. The failure is logged and the next
    run picks it up.
    """
    if not getattr(settings, "embedding_enabled", True):
        return 0
    model_name = str(getattr(settings, "embedding_model", "") or "")
    if not model_name:
        return 0

    from vts.api._helpers.artifact_store import _load_transcript_entries
    from vts.db.repo import Repo
    from vts.services.embeddings import EmbeddingClient

    try:
        repo = Repo(session)
        recording = await repo.upsert_recording_for_task(task)
        entries = _load_transcript_entries(task.artifact_dir)
        client = EmbeddingClient(
            url=str(settings.llm_url),
            api_key=settings.llm_api_key,
            model=model_name,
            timeout_seconds=int(getattr(settings, "embedding_timeout_seconds", 120)),
            batch_size=int(getattr(settings, "embedding_batch_size", 32)),
        )
        count = await index_recording(
            session, recording, entries, embedder=client, model_name=model_name
        )
        logger.info("indexed recording %s into %d chunks", recording.id, count)
        return count
    except Exception:
        logger.exception("failed to index task %s", getattr(task, "id", "?"))
        return 0

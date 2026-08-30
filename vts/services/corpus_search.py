"""Searching the transcript corpus, with a relevance threshold (vts-uurt).

The requirement that shapes this module: **below the threshold, return
nothing**. Not the k nearest rows — nothing. A vector store without a threshold
answers every query with its top-k, so a question the corpus cannot answer comes
back with confident, irrelevant passages, and a reader (human or LLM) has no way
to tell that apart from a real answer. This is built explicitly not to repeat
that behaviour.

The default was calibrated against the real deployment rather than picked:
embedding the production corpus and querying it, answerable questions scored
0.521-0.762 and unanswerable ones 0.317-0.379. The default sits inside that
band, closer to the answerable end, because a false answer costs more here than
a missed one.

**Retrieval is cross-language, and that constrains the threshold.** bge-m3 is
multilingual: verified against the Russian corpus, four English questions
returned the SAME chunks as their Russian equivalents (0.556/0.556, 0.537/0.505,
0.595/0.554, 0.495/0.493). English scores land 0.00-0.04 lower, so raising the
threshold much above the calibrated default would start dropping cross-language
hits while same-language ones still passed — a failure that would only ever
show up for users querying in another language.

Two shapes are deliberate:

* **direct top-k over raw rows**, never GROUP BY + MIN like `nearest_speakers`.
  That form aggregates every row before applying LIMIT, so an ANN index cannot
  accelerate it in principle — hnsw works on `ORDER BY embedding <=> q LIMIT k`.
* **over-fetch, then cut.** A threshold is a post-filter over an ANN result: the
  index returns k rows and the cut happens after. Fetching exactly `limit` rows
  would let the selection size decide the answer instead of the threshold,
  which is the behaviour being avoided.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Cosine similarity, not distance: 1.0 is identical. Calibrated on the real
# corpus (see the module docstring); configurable per call because a caller
# that would rather see weak matches than nothing can say so.
DEFAULT_THRESHOLD = 0.45

# How many rows to pull from the index before the threshold cuts. Generous
# relative to any sane `limit`, so the threshold is what decides the boundary.
_OVERFETCH = 8
_MAX_FETCH = 500


@dataclass(frozen=True)
class SearchHit:
    """One passage, with everything a citation needs to point back at it."""

    chunk_id: uuid.UUID
    # The stable identifier callers should hold on to. NOT the task id: a task
    # can be deleted, and the recording is what lasts (vts-8w1r).
    recording_id: uuid.UUID
    # The task the recording came from, or None once it has been deleted. Only
    # here so a caller can build /player/{task}?t= — the player is addressed by
    # task, while `recording_id` is the identifier that lasts. A null means the
    # evidence is still valid but no longer openable in a player, which the
    # caller can show instead of producing a dead link.
    source_task_id: uuid.UUID | None
    title: str | None
    text: str
    start_sec: float
    end_sec: float
    speakers: list[str]
    score: float


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.6f}" for v in embedding) + "]"


async def search_chunks(
    session: AsyncSession,
    user_id: uuid.UUID,
    embedding: list[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 10,
    recording_id: uuid.UUID | None = None,
) -> list[SearchHit]:
    """Passages matching `embedding` above `threshold`, best first.

    Returns an empty list when nothing clears the threshold — that is the
    feature, not a degenerate case.
    """
    if not embedding:
        return []
    limit = max(1, min(int(limit), 100))
    fetch = min(_MAX_FETCH, max(limit * _OVERFETCH, 50))

    params: dict[str, Any] = {
        "q": _vector_literal(embedding),
        "user_id": str(user_id),
        "fetch": fetch,
    }
    scope = ""
    if recording_id is not None:
        scope = "AND c.recording_id = :recording_id"
        params["recording_id"] = str(recording_id)

    # The ORDER BY ... LIMIT form an hnsw index can actually serve. The join to
    # recordings only decorates the rows the index already chose.
    rows = (await session.execute(text(f"""
        SELECT c.id, c.recording_id, r.source_task_id, r.title, c.text,
               c.start_sec, c.end_sec,
               c.speakers, 1 - (c.embedding <=> CAST(:q AS halfvec)) AS score
        FROM transcript_chunks c
        JOIN recordings r ON r.id = c.recording_id
        WHERE c.user_id = CAST(:user_id AS uuid)
          AND c.embedding IS NOT NULL
          {scope}
        ORDER BY c.embedding <=> CAST(:q AS halfvec)
        LIMIT :fetch
    """), params)).all()

    hits: list[SearchHit] = []
    for row in rows:
        score = float(row.score)
        # The cut. Ordered by distance, so everything after the first row below
        # the threshold is below it too.
        if score < threshold:
            break
        speakers = row.speakers
        if isinstance(speakers, str):
            import json

            try:
                speakers = json.loads(speakers)
            except ValueError:
                speakers = []
        hits.append(SearchHit(
            chunk_id=row.id,
            recording_id=row.recording_id,
            source_task_id=row.source_task_id,
            title=row.title,
            text=row.text,
            start_sec=float(row.start_sec),
            end_sec=float(row.end_sec),
            speakers=list(speakers or []),
            score=score,
        ))
        if len(hits) >= limit:
            break
    return hits


async def search_corpus(
    session: AsyncSession,
    user_id: uuid.UUID,
    query: str,
    settings: Any,
    *,
    threshold: float | None = None,
    limit: int = 10,
    recording_id: uuid.UUID | None = None,
) -> tuple[list[SearchHit], float]:
    """Embed `query` and search, returning the hits and the threshold applied.

    The single entry point for BOTH the HTTP endpoint and the MCP tool. VOS-132
    requires them to use exactly the same threshold and rules; sharing this
    function is what makes that true by construction rather than by two call
    sites agreeing today and drifting tomorrow.

    Returns the threshold alongside the hits because an empty result means
    "nothing is this relevant", and a caller cannot read that correctly without
    knowing where the bar was.
    """
    from vts.services.embeddings import EmbeddingClient

    effective = float(
        threshold
        if threshold is not None
        else getattr(settings, "search_threshold", DEFAULT_THRESHOLD)
    )
    text_query = (query or "").strip()
    if not text_query:
        return [], effective

    client = EmbeddingClient(
        url=str(settings.llm_url),
        api_key=settings.llm_api_key,
        model=str(getattr(settings, "embedding_model", "") or ""),
        timeout_seconds=int(getattr(settings, "embedding_timeout_seconds", 120)),
        batch_size=int(getattr(settings, "embedding_batch_size", 32)),
    )
    vectors = await client.embed([text_query])
    if not vectors:
        return [], effective
    hits = await search_chunks(
        session, user_id, vectors[0],
        threshold=effective, limit=limit, recording_id=recording_id,
    )
    return hits, effective

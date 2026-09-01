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

import logging
from datetime import datetime

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Cosine similarity, not distance: 1.0 is identical. Calibrated on the real
# corpus (see the module docstring); configurable per call because a caller
# that would rather see weak matches than nothing can say so.
logger = logging.getLogger(__name__)

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


def _check_offset_reachable(*, offset: int, limit: int) -> None:
    """Refuse an offset the candidate budget cannot honestly serve.

    `_MAX_FETCH` caps how many candidates are ranked, so a page starting past
    it comes back EMPTY however many results actually qualify. Combined with a
    `total` that counts the whole corpus, that reproduces exactly the silent
    lie `total` was added to remove: the response promises more and the next
    page delivers nothing.

    Raising beats returning empty because the caller can act on it — lower the
    offset, or narrow the query. Silence leaves them concluding the corpus is
    exhausted. The ceiling itself stays: it is what stops an unbounded scan.
    """
    if offset and offset + limit > _MAX_FETCH:
        raise ValueError(
            f"offset {offset} with limit {limit} exceeds the candidate ceiling "
            f"of {_MAX_FETCH}; narrow the query (person, dates, recording_id) "
            f"instead of paging further"
        )


def _scope_clauses(
    *,
    recording_id: uuid.UUID | None,
    task_ids: list[uuid.UUID] | None,
    created_from: datetime | None,
    created_to: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """The filter SQL shared by the page query and the count.

    Built once for both so `total` can never describe a different set than the
    hits it accompanies — the entire value of that number is that a client can
    trust it to mean "how many more of THESE".

    Either date bound may be omitted: an open-ended range is a real request
    ("anything since March"), and requiring both would force callers to invent
    a far-future date. `task_ids=[]` means a filter matched nobody, which is
    NOT the same as no filter — the caller must short-circuit before calling.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if recording_id is not None:
        clauses.append("AND c.recording_id = :recording_id")
        params["recording_id"] = str(recording_id)
    if task_ids is not None:
        # Chunks reach a task only through their recording.
        clauses.append("AND r.source_task_id = ANY(CAST(:task_ids AS uuid[]))")
        params["task_ids"] = [str(t) for t in task_ids]
    # Dates come from the RECORDING, never from the chunk. A chunk is created
    # when the corpus is indexed, which has nothing to do with when the
    # conversation happened: on prod 6624 of 6694 chunks carry a different day
    # from their recording (the whole corpus was indexed on one afternoon).
    # Filtering on the chunk would silently answer a different question.
    if created_from is not None:
        clauses.append("AND r.created_at >= :created_from")
        params["created_from"] = created_from
    if created_to is not None:
        clauses.append("AND r.created_at <= :created_to")
        params["created_to"] = created_to
    return " ".join(clauses), params


async def _widen_index_scan(session: AsyncSession) -> None:
    """Make the HNSW index keep scanning until the LIMIT is actually filled.

    Without this the index stops after roughly `hnsw.ef_search` candidates
    (default 40) and the query silently returns FEWER rows than asked for —
    measured on production: LIMIT 500 came back with 37 rows while 998 passages
    cleared the threshold. Nothing errors; the result is simply short, so a
    caller reads "that is all there is" from a truncated scan.

    It bites only above a certain size, which is what made it easy to dismiss:
    on a small corpus the planner prefers a sequential scan and returns
    everything, so the same query is correct for one user and truncated for
    another. The fix belongs here rather than in a migration because it is a
    property of THIS query, not of the index.

    `strict_order` keeps exact distance ordering, which the threshold cut in
    `search_chunks` depends on: it stops at the first row below the bar and
    relies on everything after being below it too.

    Set per-transaction (LOCAL), so it cannot leak into unrelated queries on a
    pooled connection. Best-effort: a backend without pgvector 0.8+ raises, and
    a short result is better than a failed search.
    """
    try:
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        await session.execute(text("SET LOCAL hnsw.max_scan_tuples = 20000"))
    except Exception:  # noqa: BLE001 - older pgvector, or no permission
        logger.debug("could not widen the vector index scan", exc_info=True)


async def count_matching_chunks(
    session: AsyncSession,
    user_id: uuid.UUID,
    embedding: list[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    recording_id: uuid.UUID | None = None,
    task_ids: list[uuid.UUID] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> int:
    """How many passages clear `threshold` in the WHOLE corpus.

    Deliberately a separate count rather than len() of the fetched page: the
    page is capped at `_MAX_FETCH`, so counting it would report the cap back as
    if it were the corpus and a caller could never tell a full result from a
    truncated one. That is the exact confusion this number exists to remove.
    """
    if not embedding:
        return 0
    await _widen_index_scan(session)
    params: dict[str, Any] = {
        "q": _vector_literal(embedding),
        "user_id": str(user_id),
        "threshold": float(threshold),
    }
    scope, extra = _scope_clauses(
        recording_id=recording_id, task_ids=task_ids,
        created_from=created_from, created_to=created_to,
    )
    params.update(extra)
    return int((await session.execute(text(f"""
        SELECT count(*) FROM transcript_chunks c
        JOIN recordings r ON r.id = c.recording_id
        WHERE c.user_id = CAST(:user_id AS uuid)
          AND c.embedding IS NOT NULL
          {scope}
          AND 1 - (c.embedding <=> CAST(:q AS halfvec)) >= :threshold
    """), params)).scalar() or 0)


async def search_chunks(
    session: AsyncSession,
    user_id: uuid.UUID,
    embedding: list[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 10,
    offset: int = 0,
    recording_id: uuid.UUID | None = None,
    task_ids: list[uuid.UUID] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> list[SearchHit]:
    """Passages matching `embedding` above `threshold`, best first.

    Returns an empty list when nothing clears the threshold — that is the
    feature, not a degenerate case.

    `offset` pages through the ranked results the same way the task and
    recording lists do. It is applied AFTER the threshold, so page 2 continues
    where page 1 stopped instead of re-ranking a different candidate set.
    """
    if not embedding:
        return []
    await _widen_index_scan(session)
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    _check_offset_reachable(offset=offset, limit=limit)
    # Fetch enough to serve this page: everything skipped still has to be
    # ranked and threshold-checked before the page can start.
    fetch = min(_MAX_FETCH, max((limit + offset) * _OVERFETCH, 50))

    params: dict[str, Any] = {
        "q": _vector_literal(embedding),
        "user_id": str(user_id),
        "fetch": fetch,
    }
    scope, extra = _scope_clauses(
        recording_id=recording_id, task_ids=task_ids,
        created_from=created_from, created_to=created_to,
    )
    params.update(extra)

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
    skipped = 0
    for row in rows:
        score = float(row.score)
        # The cut. Ordered by distance, so everything after the first row below
        # the threshold is below it too.
        if score < threshold:
            break
        # Skip AFTER the threshold check, so an offset walks the ranked,
        # qualifying results rather than the raw candidate list.
        if skipped < offset:
            skipped += 1
            continue
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
    offset: int = 0,
    recording_id: uuid.UUID | None = None,
    person: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> tuple[list[SearchHit], float, int]:
    """Embed `query` and search, returning the hits and the threshold applied.

    The single entry point for BOTH the HTTP endpoint and the MCP tool. VOS-132
    requires them to use exactly the same threshold and rules; sharing this
    function is what makes that true by construction rather than by two call
    sites agreeing today and drifting tomorrow.

    `person` and the date bounds narrow WHERE to look before relevance is
    considered. Either date bound may be given alone: "anything since March"
    is a real request, and demanding both would force a caller to invent a
    far-future date. Dates are the RECORDING's, not the chunk's — see
    `_scope_clauses`.

    Returns the threshold alongside the hits because an empty result means
    "nothing is this relevant", and a caller cannot read that correctly without
    knowing where the bar was.

    The third element is how many passages clear the threshold in the whole
    corpus, which is NOT len(hits): a page is capped by `limit`. Without it a
    caller cannot distinguish "that was everything" from "there is more" — and
    would have to guess by whether the page came back full, which is wrong
    exactly when the total is an exact multiple of the limit.
    """
    from vts.services.embeddings import EmbeddingClient

    effective = float(
        threshold
        if threshold is not None
        else getattr(settings, "search_threshold", DEFAULT_THRESHOLD)
    )
    text_query = (query or "").strip()
    if not text_query:
        return [], effective, 0

    client = EmbeddingClient(
        url=str(settings.llm_url),
        api_key=settings.llm_api_key,
        model=str(getattr(settings, "embedding_model", "") or ""),
        timeout_seconds=int(getattr(settings, "embedding_timeout_seconds", 120)),
        batch_size=int(getattr(settings, "embedding_batch_size", 32)),
    )
    vectors = await client.embed([text_query])
    if not vectors:
        return [], effective, 0

    # A person resolves to the tasks they were identified in. Voice
    # identification hangs off the task, so a recording whose task was deleted
    # can never match — correctly: nothing records who spoke in it any more.
    task_ids: list[uuid.UUID] | None = None
    if person and person.strip():
        from vts.db.repo import Repo

        repo = Repo(session)
        found: list[uuid.UUID] = []
        for sp in await repo.speakers_by_name(user_id, person):
            found.extend(await repo.tasks_featuring_speaker(user_id, sp.id))
        task_ids = list(dict.fromkeys(found))
        # Nobody by that name, or they appear nowhere: an empty result is the
        # honest answer. Passing an empty list on would filter to nothing
        # anyway, but returning here also skips a pointless vector scan.
        if not task_ids:
            return [], effective, 0

    hits = await search_chunks(
        session, user_id, vectors[0],
        threshold=effective, limit=limit, offset=offset,
        recording_id=recording_id, task_ids=task_ids,
        created_from=created_from, created_to=created_to,
    )
    total = await count_matching_chunks(
        session, user_id, vectors[0],
        threshold=effective, recording_id=recording_id, task_ids=task_ids,
        created_from=created_from, created_to=created_to,
    )
    return hits, effective, total

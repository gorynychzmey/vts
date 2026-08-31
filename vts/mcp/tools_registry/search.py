"""Corpus search as an MCP tool (vts-uurt / VOS-132).

Deliberately the same code path as the HTTP endpoint: both call
`services.corpus_search.search_corpus`, so the threshold and the relevance
rules cannot drift apart. VOS-132 requires that explicitly, and two call sites
that merely agree today would not stay agreed.

The tool returns EVIDENCE — passages, positions, scores — and never a composed
answer. VTS is a retrieval server; the reasoning belongs to the client.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastmcp import FastMCP

from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import READ_ONLY
from vts.mcp.schemas import SearchHit, SearchResult
from vts.services.corpus_search import search_corpus


def hit_links(
    *,
    recording_id: Any,
    source_task_id: Any,
    start_sec: float,
    base_url: str | None,
) -> dict[str, str | None]:
    """The two ways to follow a hit, and they do not last equally.

    * `transcript_url` reads the passage from the RECORDING. It keeps working
      after the job that produced it is deleted, because a recording owns its
      artifacts — this is the one to use for reading.
    * `player_url` opens that second in the media for a person to watch. It is
      addressed by TASK, so it 404s once the task is gone, and is therefore
      None in that case rather than handed over as a dead link.

    Returning both, with the missing one explicitly null, means a client never
    has to guess which kind of URL it is holding — or assemble one itself and
    get the lifetime wrong.
    """
    root = (base_url or "").rstrip("/")
    at = int(float(start_sec or 0))
    transcript = (
        f"{root}/api/recordings/{recording_id}/transcript"
        f"?around_sec={at}&window_sec=60"
    )
    player = f"{root}/player/{source_task_id}?t={at}" if source_task_id else None
    return {"transcript_url": transcript, "player_url": player}


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""

    @mcp.tool(name="search_transcripts", annotations=READ_ONLY)
    async def _search_transcripts(
        query: str,
        limit: int = 10,
        threshold: float | None = None,
        recording_id: uuid.UUID | None = None,
    ) -> SearchResult:
        """Search your recordings for passages relevant to a question.

        Returns matching passages with their recording, speakers, timecodes and
        relevance score — evidence to reason over, not an answer.

        When nothing in the corpus is relevant enough, `hits` is EMPTY. That is
        a real answer: it means the recordings do not cover the question. Do not
        treat an empty result as a failure, and do not lower `threshold` to
        manufacture matches — the returned `threshold` tells you where the bar
        was.

        Each hit carries two links, and they do not last equally:

        * `transcript_url` reads the passage from the RECORDING — use this to
          expand a quote, or call `get_recording_transcript` with the hit's
          `recording_id` and `around_sec`. It keeps working after the job that
          produced the recording has been deleted.
        * `player_url` opens that second in the media for a person to watch.
          It is addressed by task, so it is null once the task is gone — offer
          it when present, and do not construct it yourself.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            hits, effective = await search_corpus(
                session, uuid.UUID(user.id), query, settings,
                threshold=threshold, limit=limit, recording_id=recording_id,
            )
            base_url = str(getattr(settings, "public_base_url", "") or "")
            return SearchResult(
                query=query,
                threshold=effective,
                hits=[
                    SearchHit(
                        recording_id=h.recording_id,
                        source_task_id=h.source_task_id,
                        title=h.title,
                        text=h.text,
                        start_sec=h.start_sec,
                        end_sec=h.end_sec,
                        speakers=h.speakers,
                        score=h.score,
                        **hit_links(
                            recording_id=h.recording_id,
                            source_task_id=h.source_task_id,
                            start_sec=h.start_sec,
                            base_url=base_url,
                        ),
                    )
                    for h in hits
                ],
            )

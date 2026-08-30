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

from fastmcp import FastMCP

from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.schemas import SearchHit, SearchResult
from vts.services.corpus_search import search_corpus


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""

    @mcp.tool(name="search_transcripts")
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

        Use `recording_id` from a hit to fetch that recording's full transcript.

        To cite a hit so a person can verify it, link to
        `/player/{source_task_id}?t={start_sec}` — that opens the recording at
        the quoted passage with it highlighted. When `source_task_id` is null
        the task has been deleted: the passage and its timecode are still real,
        but there is no player page to link to, so quote it without a link
        rather than inventing one.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, settings = await mcp_authenticate(session)
            hits, effective = await search_corpus(
                session, uuid.UUID(user.id), query, settings,
                threshold=threshold, limit=limit, recording_id=recording_id,
            )
            return SearchResult(
                query=query,
                threshold=effective,
                hits=[
                    SearchHit(
                        recording_id=h.recording_id, source_task_id=h.source_task_id, title=h.title, text=h.text,
                        start_sec=h.start_sec, end_sec=h.end_sec,
                        speakers=h.speakers, score=h.score,
                    )
                    for h in hits
                ],
            )

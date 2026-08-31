"""Recordings as MCP tools (vts-lib3).

The task tools came first, when a task WAS the recording. Since the split a
task is a job and a recording is what it produced — and `search_transcripts`
already returns `recording_id` as the identifier to keep. Without these tools
that identifier could not be used to read anything, so an assistant that found
a passage had to hop back to a task that may no longer exist.

These are the tools for the ARCHIVE. The task-scoped ones remain for asking
about a run in progress.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from fastmcp import FastMCP
from sqlalchemy import func, select

from vts.db.models import Recording
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import READ_ONLY
from vts.mcp.schemas import (
    RecordingInfo,
    RecordingList,
    RecordingTranscript,
    TranscriptEntry,
)
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    read_recording_transcript,
    recording_transcript_entries,
)


def _info(recording: Recording) -> RecordingInfo:
    from pathlib import Path

    from vts.api._helpers.base import _find_media_file

    transcript = recording.transcript_path
    summary = recording.summary_path
    return RecordingInfo(
        id=recording.id,
        source_task_id=recording.source_task_id,
        title=recording.title,
        duration_sec=recording.duration_sec,
        language=recording.language,
        has_transcript=bool(transcript and Path(transcript).exists()),
        has_summary=bool(summary and Path(summary).exists()),
        has_media=_find_media_file(recording.artifact_dir) is not None,
        created_at=recording.created_at,
    )


def register(mcp: FastMCP) -> None:
    """Register this domain's tools on `mcp`."""

    @mcp.tool(name="list_recordings", annotations=READ_ONLY)
    async def _list_recordings(limit: int = 50, offset: int = 0) -> RecordingList:
        """List the recordings in the library, newest first.

        A recording is a processed video or audio file that stays available
        after the job that produced it is gone. Use `id` from here with
        `get_recording_transcript`.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            repo = Repo(session)
            items = await repo.list_recordings(
                user_id, limit=max(1, min(int(limit), 200)), offset=max(0, int(offset))
            )
            total = await session.scalar(
                select(func.count()).select_from(Recording).where(Recording.user_id == user_id)
            )
            return RecordingList(items=[_info(r) for r in items], total=int(total or 0))

    @mcp.tool(name="get_recording_transcript", annotations=READ_ONLY)
    async def _get_recording_transcript(
        recording_id: uuid.UUID,
        variant: str = "raw",
        structured: bool = False,
        around_sec: float | None = None,
        window_sec: float = 60.0,
    ) -> RecordingTranscript:
        """Read a recording's transcript or summary.

        This is the tool to use after `search_transcripts`: pass the hit's
        `recording_id`. It works even when the originating task has been
        deleted — the recording keeps its own artifacts.

        `variant` is "raw" (the transcript), "redacted" (the processed one) or
        "summary".

        Set `structured=true` to get `entries` with timecodes and speakers
        instead of flat text — that is what lets you cite a passage rather than
        only quote it.

        With `around_sec` (and `structured=true`) only the passage around that
        second is returned. Prefer that when expanding a search hit: fetching a
        two-hour transcript to show one quote buries the part that mattered.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            repo = Repo(session)
            recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")

            kind = variant if variant in {"raw", "redacted", "summary"} else "raw"
            try:
                if structured or around_sec is not None:
                    entries = recording_transcript_entries(
                        recording, around_sec=around_sec, window_sec=window_sec
                    )
                    return RecordingTranscript(
                        recording_id=recording.id,
                        title=recording.title,
                        variant=kind,
                        around_sec=around_sec,
                        entries=[
                            TranscriptEntry(
                                start_sec=float(e.get("start") or 0.0),
                                end_sec=float(e.get("end") or 0.0),
                                text=str(e.get("text") or ""),
                                speaker=(str(e["speaker"]) if e.get("speaker") else None),
                            )
                            for e in entries
                        ],
                    )
                return RecordingTranscript(
                    recording_id=recording.id,
                    title=recording.title,
                    variant=kind,
                    content=read_recording_transcript(recording, kind),
                )
            except RecordingArtifactMissing as exc:
                # 404 with the reason: "archived away" and "never produced" are
                # different answers, and a client should not have to guess.
                raise HTTPException(status_code=404, detail=str(exc)) from exc

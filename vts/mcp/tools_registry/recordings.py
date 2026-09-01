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
from datetime import datetime

from fastapi import HTTPException
from fastmcp import FastMCP
from sqlalchemy import func, select

from vts.db.models import MatchDecision, Recording, Speaker
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.auth import mcp_authenticate
from vts.mcp.annotations import DESTRUCTIVE, READ_ONLY, UPDATE
from vts.mcp.schemas import (
    PeopleList,
    PromptResult,
    PersonInfo,
    RecordingInfo,
    RecordingList,
    RecordingTranscript,
    TranscriptEntry,
)
from vts.services.subtitles import render_webvtt
from vts.api._helpers.recordings import rename_recording
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    read_recording_transcript,
    recording_transcript_entries,
)


def _info(recording: Recording, *, people: list[str] | None = None) -> RecordingInfo:
    from pathlib import Path

    from vts.api._helpers.base import _find_media_file

    transcript = recording.transcript_path
    summary = recording.summary_path
    return RecordingInfo(
        id=recording.id,
        source_task_id=recording.source_task_id,
        people=list(people or []),
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
    async def _list_recordings(
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        diarized: bool | None = None,
        person: str | None = None,
    ) -> RecordingList:
        """List the recordings in the library, newest first.

        A recording is a processed video or audio file that stays available
        after the job that produced it is gone. Use `id` from here with
        `get_recording_transcript`.

        Each item carries `people` — the names identified by voice, empty when
        the recording was not diarised. Filter with `person` (a name, matched
        case-insensitively on any part) or `diarized` to get only recordings
        that do or do not have identified voices.

        `total` counts everything matching the filters, not this page, so
        `offset` + len(items) < total means there is more to fetch.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            repo = Repo(session)

            stmt = select(Recording).where(Recording.user_id == user_id)
            count_stmt = (
                select(func.count()).select_from(Recording)
                .where(Recording.user_id == user_id)
            )
            if q:
                like = f"%{q.strip()}%"
                cond = Recording.title.ilike(like)
                stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
            if created_from is not None:
                cond = Recording.created_at >= created_from
                stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)
            if created_to is not None:
                cond = Recording.created_at <= created_to
                stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)

            # Voice identification hangs off the TASK, so both voice filters
            # resolve to a set of task ids and then constrain the recordings
            # that still point at one. A recording whose task was deleted can
            # never satisfy them — correctly: its people are unknowable now.
            if person or diarized is not None:
                named = (
                    select(MatchDecision.source_task_id)
                    .where(
                        MatchDecision.user_id == user_id,
                        MatchDecision.speaker_id.isnot(None),
                        MatchDecision.source_task_id.isnot(None),
                    )
                )
                if person:
                    named = named.join(
                        Speaker, MatchDecision.speaker_id == Speaker.id
                    ).where(Speaker.name.ilike(f"%{person.strip()}%"))
                subq = named.distinct().scalar_subquery()
                cond = (
                    Recording.source_task_id.in_(subq)
                    if (person or diarized)
                    else Recording.source_task_id.notin_(subq)
                )
                stmt, count_stmt = stmt.where(cond), count_stmt.where(cond)

            stmt = (
                stmt.order_by(Recording.created_at.desc())
                .limit(max(1, min(int(limit), 200)))
                .offset(max(0, int(offset)))
            )
            items = list((await session.execute(stmt)).scalars().all())
            total = int(await session.scalar(count_stmt) or 0)

            task_ids = [r.source_task_id for r in items if r.source_task_id]
            people = await repo.speaker_names_for_tasks(user_id, task_ids)
            return RecordingList(
                items=[
                    _info(r, people=people.get(r.source_task_id, []))
                    for r in items
                ],
                total=total,
            )

    @mcp.tool(name="list_people", annotations=READ_ONLY)
    async def _list_people() -> PeopleList:
        """List the people in the voice registry.

        These are the names that voice identification can attach to a speaker.
        Use a name with `list_recordings(person=...)` or `list_tasks(person=...)`
        to find where someone appears.

        `task_count` is 0 for a person known only from deleted tasks: the
        identification outlives the job, but no longer points at one. Such a
        person is real and named, just not reachable by filtering.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            counts = dict((
                await session.execute(
                    select(
                        MatchDecision.speaker_id,
                        func.count(func.distinct(MatchDecision.source_task_id)),
                    )
                    .where(
                        MatchDecision.user_id == user_id,
                        MatchDecision.speaker_id.isnot(None),
                        MatchDecision.source_task_id.isnot(None),
                    )
                    .group_by(MatchDecision.speaker_id)
                )
            ).all())
            people = await Repo(session).list_speakers(user_id)
            return PeopleList(
                items=[
                    PersonInfo(
                        id=p.id,
                        name=p.name,
                        task_count=int(counts.get(p.id, 0)),
                        created_at=getattr(p, "created_at", None),
                    )
                    for p in people
                ],
                total=len(people),
            )

    @mcp.tool(name="get_recording_prompt_result", annotations=READ_ONLY)
    async def _get_recording_prompt_result(
        recording_id: uuid.UUID, ref: str = "system:summary",
    ) -> PromptResult:
        """Read one prompt result (a summary, a memo) of a RECORDING.

        The recording-scoped counterpart of `get_prompt_result`. Prefer it
        after `search_transcripts`: the results are snapshotted onto the
        recording, so they survive the deletion of the job that produced them,
        while the task-scoped tool 404s once the task is gone.

        `ref` is a "source:id" string — "system:summary" for the markdown
        summary, or "user:<prompt-uuid>" for a custom prompt.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            recording = await Repo(session).get_recording_for_user(
                uuid.UUID(user.id), recording_id
            )
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
            results = (recording.meta or {}).get("prompt_results") or []
            wanted = (ref or "").strip() or "system:summary"
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                entry_ref = f"{entry.get('source', '')}:{entry.get('id', '')}"
                if entry_ref == wanted or entry.get("ref") == wanted:
                    source, _, ident = wanted.partition(":")
                    return PromptResult(
                        task_id=recording.source_task_id,
                        source=source,
                        id=ident,
                        content=entry.get("text") or entry.get("result") or "",
                    )
            # Falling back to the summary artifact: the oldest recordings were
            # created before results were snapshotted into meta, and for them
            # the file on disk is the only copy.
            if wanted == "system:summary":
                try:
                    return PromptResult(
                        task_id=recording.source_task_id,
                        source="system",
                        id="summary",
                        content=read_recording_transcript(recording, "summary"),
                    )
                except RecordingArtifactMissing:
                    pass
            available = [
                f"{e.get('source', '')}:{e.get('id', '')}"
                for e in results if isinstance(e, dict)
            ]
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No prompt result {wanted!r} for this recording"
                    + (f"; available: {', '.join(available)}" if available else "")
                ),
            )

    @mcp.tool(name="rename_recording", annotations=UPDATE)
    async def _rename_recording(recording_id: uuid.UUID, title: str) -> RecordingInfo:
        """Give a recording a name of its own.

        The name sticks: it is marked custom, so later processing of the same
        source will not overwrite it. Renaming a recording does NOT rename the
        task that produced it — they are separate objects, and the recording is
        the one that lasts.
        """
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            repo = Repo(session)
            recording = await repo.get_recording_for_user(
                uuid.UUID(user.id), recording_id
            )
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
            await rename_recording(session, recording, title)
            await session.commit()
            await session.refresh(recording)
            return _info(recording)

    @mcp.tool(name="get_recording_transcript", annotations=READ_ONLY)
    async def _get_recording_transcript(
        recording_id: uuid.UUID,
        variant: str = "raw",
        structured: bool = False,
        as_subtitles: bool = False,
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

        Set `as_subtitles=true` to get the same passage as WebVTT — one text
        with timecodes and speaker names in it, ready to hand to a player or
        quote with positions. It honours `around_sec`, so a hit can be turned
        into a short subtitle excerpt rather than a whole file.
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
                if as_subtitles:
                    # Subtitles are a RENDERING of the same entries, not a
                    # separate artifact — so the window and speaker names come
                    # out identical to the structured form by construction.
                    entries = recording_transcript_entries(
                        recording, around_sec=around_sec, window_sec=window_sec
                    )
                    return RecordingTranscript(
                        recording_id=recording.id,
                        title=recording.title,
                        variant=kind,
                        around_sec=around_sec,
                        # render_webvtt takes player BLOCKS ({label, sentences}),
                        # not the flat {start, end, text, speaker} entries — one
                        # block per entry keeps each line its own cue.
                        text=render_webvtt([
                            {
                                "label": e.get("speaker") or "",
                                "sentences": [{
                                    "start": e.get("start"),
                                    "end": e.get("end"),
                                    "text": e.get("text", ""),
                                }],
                            }
                            for e in entries
                        ]),
                        entries=[],
                    )
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

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

import logging
import uuid
from datetime import datetime
from typing import Literal

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
from vts.services.diarization.merge import label_map, speaker_label_word
from vts.services.subtitles import render_webvtt
from vts.api._helpers.artifact_store import recording_label_language
from vts.api._helpers.recordings import rename_recording
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    read_recording_result,
    read_recording_transcript,
    recording_result_entries,
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
        state=recording.state.value,
        created_at=recording.created_at,
        listed_at=recording.listed_at,
    )


logger = logging.getLogger(__name__)


def recording_prompt_result(recording: Recording, ref: str) -> PromptResult:
    """One prompt result of a recording (a summary, a memo), by "source:id".

    Shared by `get_recording_prompt_result` and the MCP Apps summary view
    (vts-vtma.5). Raises 404 naming the refs that do exist.
    """
    wanted = (ref or "").strip() or "system:summary"
    source, _, ident = wanted.partition(":")
    # The entry stores a PATH, not the text: a reader looking for a "text" key
    # returned 200 with empty content once, and a caller cannot tell an empty
    # summary from a broken reader. The oldest recordings have no entry for the
    # summary at all; for them `summary_path` is the only copy. No stored path
    # means "not ready", as over HTTP: a default-named outputs/summary.md left
    # behind by a restart is the summary being replaced (vts-lzad).
    try:
        content = read_recording_result(recording, source, ident)
    except RecordingArtifactMissing:
        content = None
    if content is not None:
        return PromptResult(
            task_id=recording.source_task_id,
            source=source,
            id=ident,
            content=content,
        )
    available = [
        f"{e.get('source', '')}:{e.get('id', '')}"
        for e in recording_result_entries(recording)
    ]
    raise HTTPException(
        status_code=404,
        detail=(
            f"No prompt result {wanted!r} for this recording"
            + (f"; available: {', '.join(available)}" if available else "")
        ),
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
        before_ts: datetime | None = None,
        before_id: uuid.UUID | None = None,
        after_ts: datetime | None = None,
        after_id: uuid.UUID | None = None,
        order: Literal["asc", "desc"] = "desc",
        task_ids: list[uuid.UUID] | None = None,
    ) -> RecordingList:
        """List the recordings in the library, newest first (by when each got
        its text, `listed_at`).

        Page with `limit`/`offset`, or with cursors: `before_ts` + `before_id`
        (the `listed_at` and `id` of the last item seen) for older ones,
        `after_ts` + `after_id` with `order="asc"` for newer ones. `task_ids`
        keeps only the recordings of those tasks.

        A recording is a processed video or audio file that stays available
        after the job that produced it is gone. Use `id` from here with
        `get_recording_transcript`. Recordings without any transcript yet are
        not listed; each item's `state` says whether its results are final.

        Each item carries `people` — the names identified by voice, empty when
        the recording was not diarised. Filter with `person` (a name, matched
        case-insensitively on any part) or `diarized` to get only recordings
        that do or do not have identified voices.

        `total` counts everything matching the filters, not this page. With
        `offset` paging, `offset` + len(items) < total means there is more to
        fetch; with cursors, a page shorter than `limit` means the end.
        """
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        if (before_ts is None) != (before_id is None):
            raise ValueError("before_ts and before_id must be supplied together")
        if (after_ts is None) != (after_id is None):
            raise ValueError("after_ts and after_id must be supplied together")
        before = (before_ts, before_id) if before_ts is not None else None
        after = (after_ts, after_id) if after_ts is not None else None
        session_factory = get_db_session_factory()
        async with session_factory() as session:
            user, _settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            repo = Repo(session)

            # Voice identification hangs off the TASK, so both voice filters
            # resolve to a set of task ids the repo then constrains on.
            requested_task_ids = task_ids
            task_ids = exclude_ids = None
            if person or diarized is not None:
                if person:
                    matched: list[uuid.UUID] = []
                    for sp in await repo.speakers_by_name(user_id, person):
                        matched.extend(
                            await repo.tasks_featuring_speaker(user_id, sp.id)
                        )
                    ids = list(dict.fromkeys(matched))
                else:
                    ids = await repo.diarized_task_ids(user_id)
                if person or diarized:
                    # Nobody matched: an empty page is the honest answer, not
                    # the unfiltered list.
                    if not ids:
                        return RecordingList(items=[], total=0)
                    task_ids = ids
                else:
                    exclude_ids = ids
            if requested_task_ids is not None:
                # Both an explicit set and a voice filter: the recordings must
                # satisfy both, so intersect rather than let one win.
                wanted = set(requested_task_ids)
                task_ids = (
                    list(requested_task_ids) if task_ids is None
                    else [t for t in task_ids if t in wanted]
                )
                if not task_ids:
                    return RecordingList(items=[], total=0)

            filters = {
                "q": q, "created_from": created_from, "created_to": created_to,
                "task_ids": task_ids, "exclude_task_ids": exclude_ids,
            }
            items = await repo.list_recordings(
                user_id,
                limit=max(1, min(int(limit), 200)),
                offset=max(0, int(offset)),
                before=before, after=after, order=order,
                **filters,
            )
            total = await repo.count_recordings(user_id, **filters)

            people = await repo.speaker_names_for_tasks(
                user_id, [r.source_task_id for r in items if r.source_task_id]
            )
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
            return recording_prompt_result(recording, ref)

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
                    # A voice the recording's task gave a name speaks under it;
                    # an unnamed one gets the localized speaker word, numbered
                    # by first appearance in the WHOLE recording — the label
                    # the player and the HTTP subtitles show, never the raw
                    # SPEAKER_NN (vts-lzad). Names belong to the task, so a
                    # recording whose task is gone has none.
                    names: dict[str, str] = {}
                    if recording.source_task_id is not None:
                        names = await repo.speaker_names_for_task(
                            recording.user_id, recording.source_task_id
                        )
                    labels = label_map(
                        recording_transcript_entries(recording),
                        speaker_label_word(recording_label_language(recording)),
                        names=names,
                    )
                    return RecordingTranscript(
                        recording_id=recording.id,
                        title=recording.title,
                        variant=kind,
                        around_sec=around_sec,
                        # render_webvtt takes player BLOCKS ({label, sentences}),
                        # not the flat {start, end, text, speaker} entries — one
                        # block per entry keeps each line its own cue.
                        content=render_webvtt([
                            {
                                "label": labels.get(e.get("speaker") or "", ""),
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

"""The knowledge library: recordings that outlive the tasks that made them.

A Recording (vts-8w1r / VOS-130) is the lasting object — a task is one way of
creating or updating it. These endpoints are read-only for now: creation happens
in the pipeline, and the library lists what has been produced.

Access is owner-scoped exactly as tasks are: every read goes through
get_recording_for_user / list_recordings, which filter on user_id. Sharing a
recording is not part of this.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.base import _find_media_file
from vts.api._helpers.recordings import rename_recording
from vts.api.deps import get_current_user, get_session_dep, get_settings_dep
from vts.api.schemas import (
    RecordingListOut,
    RecordingOut,
    RecordingTranscriptOut,
    SearchHitOut,
    SearchResultOut,
    TranscriptEntryOut,
)
from vts.db.models import Recording
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.corpus_search import search_corpus
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    read_recording_transcript,
    recording_transcript_entries,
)

router = APIRouter()


def _serialize(recording: Recording) -> RecordingOut:
    """A recording as the library shows it.

    The three `has_*` flags are probed from disk rather than stored: archiving
    removes the media (and, for an archived task, the transcript stays but the
    rest goes), so a stored flag would go stale the moment a recording is
    archived. What is NOT probed is duration and language — those are columns
    precisely because they must survive the files.
    """
    transcript = recording.transcript_path
    summary = recording.summary_path
    root = Path(recording.artifact_dir or "")
    redacted = root / "outputs" / "redacted_transcript.txt"
    meta = recording.meta if isinstance(recording.meta, dict) else {}
    prompt_results = meta.get("prompt_results")
    return RecordingOut(
        id=recording.id,
        source_task_id=recording.source_task_id,
        title=recording.title,
        title_is_custom=bool(recording.title_is_custom),
        source_url=recording.source_url,
        duration_sec=recording.duration_sec,
        language=recording.language,
        tags=list(recording.tags or []),
        has_transcript=bool(transcript and Path(transcript).exists()),
        has_redacted=redacted.exists(),
        has_summary=bool(summary and Path(summary).exists()),
        has_media=_find_media_file(recording.artifact_dir) is not None,
        prompt_results=[r for r in (prompt_results or []) if isinstance(r, dict)],
        recorded_at=recording.recorded_at,
        created_at=recording.created_at,
        updated_at=recording.updated_at,
    )


@router.get("/api/recordings", response_model=RecordingListOut)
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> RecordingListOut:
    """The user's recordings, newest first."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    repo = Repo(session)
    user_id = uuid.UUID(user.id)
    items = await repo.list_recordings(user_id, limit=limit, offset=offset)
    total = await session.scalar(
        select(func.count()).select_from(Recording).where(Recording.user_id == user_id)
    )
    return RecordingListOut(items=[_serialize(r) for r in items], total=int(total or 0))


@router.get("/api/recordings/{recording_id}", response_model=RecordingOut)
async def get_recording(
    recording_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> RecordingOut:
    """One recording. 404 for another user's, same as tasks."""
    repo = Repo(session)
    recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return _serialize(recording)


@router.patch("/api/recordings/{recording_id}", response_model=RecordingOut)
async def rename_recording_endpoint(
    recording_id: uuid.UUID,
    display_name: str | None = Body(default=None, embed=True),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> RecordingOut:
    """Give a recording a name of its own.

    Marks it as chosen, so neither a task rename nor the next pipeline run
    replaces it. Sending an empty name clears that and restores the derived
    one — the way back, without which naming a recording once would cut it off
    from its task for good.
    """
    repo = Repo(session)
    recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    await rename_recording(session, recording, display_name)
    await session.commit()
    return _serialize(recording)


@router.get("/api/search", response_model=SearchResultOut)
async def search_corpus_endpoint(
    q: str,
    limit: int = 10,
    threshold: float | None = None,
    recording_id: uuid.UUID | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    settings=Depends(get_settings_dep),
) -> SearchResultOut:
    """Search the transcript corpus (vts-uurt).

    Returns nothing when nothing clears the relevance threshold, rather than
    the nearest passages. The threshold is echoed back so an empty result can
    be read correctly.
    """
    hits, effective = await search_corpus(
        session, uuid.UUID(user.id), q, settings,
        threshold=threshold, limit=limit, recording_id=recording_id,
    )
    return SearchResultOut(
        query=q,
        threshold=effective,
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id, recording_id=h.recording_id, source_task_id=h.source_task_id, title=h.title,
                text=h.text, start_sec=h.start_sec, end_sec=h.end_sec,
                speakers=h.speakers, score=h.score,
            )
            for h in hits
        ],
    )


@router.get(
    "/api/recordings/{recording_id}/transcript",
    response_model=RecordingTranscriptOut,
)
async def get_recording_transcript_endpoint(
    recording_id: uuid.UUID,
    variant: str = "raw",
    around_sec: float | None = None,
    window_sec: float = 60.0,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> RecordingTranscriptOut:
    """A recording's transcript. With `around_sec`, only the passage around
    that second. Works after the originating task has been deleted."""
    # The docstring becomes the OpenAPI operation description, capped at 300
    # chars for ChatGPT Actions (tests/test_openapi_spec.py), so the rationale
    # lives here: a recording keeps its own artifacts, which is what makes this
    # answer when /player/{task_id} would 404. The window is what expanding a
    # search hit needs — showing one quote in context should not mean loading a
    # two-hour transcript.
    repo = Repo(session)
    recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    kind = variant if variant in {"raw", "redacted", "summary"} else "raw"
    try:
        if around_sec is not None:
            entries = recording_transcript_entries(
                recording, around_sec=around_sec, window_sec=window_sec
            )
            return RecordingTranscriptOut(
                recording_id=recording.id,
                title=recording.title,
                variant=kind,
                around_sec=around_sec,
                entries=[
                    TranscriptEntryOut(
                        start_sec=float(e.get("start") or 0.0),
                        end_sec=float(e.get("end") or 0.0),
                        text=str(e.get("text") or ""),
                        speaker=(str(e["speaker"]) if e.get("speaker") else None),
                    )
                    for e in entries
                ],
            )
        return RecordingTranscriptOut(
            recording_id=recording.id,
            title=recording.title,
            variant=kind,
            content=read_recording_transcript(recording, kind),
        )
    except RecordingArtifactMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

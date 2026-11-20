"""The knowledge library: recordings that outlive the tasks that made them.

A Recording (vts-8w1r / VOS-130) is the lasting object — a task is one way of
creating or updating it. These endpoints are read-only for now: creation happens
in the pipeline, and the library lists what has been produced.

Access is owner-scoped exactly as tasks are: every read goes through
get_recording_for_user / list_recordings, which filter on user_id. Sharing a
recording is not part of this.
"""
from __future__ import annotations

from datetime import datetime
import uuid
from pathlib import Path

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.base import _find_media_file
from vts.api._helpers.recording_content import (
    TextVariant,
    recording_entries,
    recording_for_user_or_404,
    recording_result,
    recording_subtitles,
    serve_recording_text,
)
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
from vts.core.config import Settings
from vts.db.models import Recording
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.corpus_search import search_corpus
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    _artifact_path,
    read_recording_transcript,
    recording_result_entries,
    recording_transcript_entries,
)

router = APIRouter()


def _serialize(recording: Recording) -> RecordingOut:
    """A recording as the library shows it.

    The four `has_*` flags are probed from disk rather than stored: archiving
    removes the media (and, for an archived task, the transcript stays but the
    rest goes), so a stored flag would go stale the moment a recording is
    archived. What is NOT probed is duration and language — those are columns
    precisely because they must survive the files.
    """
    transcript = recording.transcript_path
    summary = recording.summary_path
    # No directory means no artifacts, full stop. `Path("")` is `Path(".")`, so
    # the probe below used to read ./outputs/... — relative to wherever the
    # process was started, which is not a property of the recording at all
    # (same shape as artifacts_removable_for_task, which returns False here).
    artifact_dir = recording.artifact_dir or ""
    redacted = _artifact_path(recording, "redacted") if (
        artifact_dir or recording.redacted_path
    ) else None
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
        state=recording.state.value,
        has_redacted=bool(redacted and redacted.exists()),
        has_summary=bool(summary and Path(summary).exists()),
        has_media=_find_media_file(recording.artifact_dir) is not None,
        prompt_results=recording_result_entries(recording),
        recorded_at=recording.recorded_at,
        created_at=recording.created_at,
        listed_at=recording.listed_at,
        updated_at=recording.updated_at,
    )


@router.get("/api/recordings", response_model=RecordingListOut)
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    before_ts: datetime | None = None,
    before_id: uuid.UUID | None = None,
    after_ts: datetime | None = None,
    after_id: uuid.UUID | None = None,
    order: str = "desc",
    task_id: list[uuid.UUID] | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> RecordingListOut:
    """The user's recordings, newest first by when each got text, filtered
    server-side. Page with `limit`/`offset` or the `before_*`/`after_*`
    cursors plus `order`; `task_id` (repeatable) filters by task.
    """
    # The filters live here rather than in the browser because the client only
    # holds the pages it has loaded: filtering there answers "not among the 30
    # you have", which is indistinguishable to the user from "does not exist".
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="order must be 'asc' or 'desc'")
    if (before_ts is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_ts and before_id must be supplied together")
    if (after_ts is None) != (after_id is None):
        raise HTTPException(status_code=422, detail="after_ts and after_id must be supplied together")
    before = (before_ts, before_id) if before_ts is not None else None
    after = (after_ts, after_id) if after_ts is not None else None
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    repo = Repo(session)
    user_id = uuid.UUID(user.id)
    filters = {
        "q": q, "created_from": created_from, "created_to": created_to,
        "task_ids": task_id or None,
    }
    items = await repo.list_recordings(
        user_id, limit=limit, offset=offset, before=before, after=after,
        order=order, **filters,
    )
    # Counted with the SAME filters, so the number beside the heading describes
    # the result set rather than the corpus.
    total = await repo.count_recordings(user_id, **filters)
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


@router.get("/api/recordings/{recording_id}/media", include_in_schema=False)
async def get_recording_media(
    recording_id: uuid.UUID,
    u: uuid.UUID,
    exp: int,
    sig: str,
    session: AsyncSession = Depends(get_session_dep),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    """A recording's media through a signed link (vts-vtma.3).

    No session: the MCP player view plays this from the host's sandboxed
    iframe, which carries neither our cookie nor a bearer token. The link
    authorises itself instead — signed for this recording and user, valid for
    minutes, issued only by the app_media_url tool after the normal per-user
    check. Ownership is still checked here against `u`, so a link is useless
    for another user's recording even with a valid signature.

    Every refusal is the same 404, so a probe cannot tell a bad signature from
    a missing recording.
    """
    from vts.services.media_kind import media_content_type
    from vts.services.media_urls import media_signing_key, verify_media_signature

    not_found = HTTPException(status_code=404, detail="Media not available")
    if not verify_media_signature(media_signing_key(settings), recording_id, u, exp, sig):
        raise not_found
    recording = await Repo(session).get_recording_for_user(u, recording_id)
    if recording is None:
        raise not_found
    media_file = _find_media_file(recording.artifact_dir)
    if media_file is None:
        raise not_found
    return FileResponse(
        path=str(media_file),
        media_type=media_content_type(media_file),
        # The link is personal and short-lived; nothing in between should keep it.
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.get("/api/recordings/{recording_id}/speaker-previews/{speaker_label}/{index}/audio", include_in_schema=False)
async def get_recording_speaker_preview(
    recording_id: uuid.UUID,
    speaker_label: str,
    index: int,
    u: uuid.UUID,
    exp: int,
    sig: str,
    request: Request,
    session: AsyncSession = Depends(get_session_dep),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    """A voice's preview clip through a signed link (vts-vtma.6), for the MCP
    speaker view — same reasoning as get_recording_media: the host's iframe
    carries no session. The signature covers this exact path, so a link opens
    one clip of one voice and nothing else; every refusal is the same 404.
    """
    from vts.services.media_urls import media_signing_key, verify_path_signature
    from vts.services.speaker_resolution import preview_clip_path

    not_found = HTTPException(status_code=404, detail="Clip not available")
    if not verify_path_signature(media_signing_key(settings), request.url.path, u, exp, sig):
        raise not_found
    recording = await Repo(session).get_recording_for_user(u, recording_id)
    if recording is None:
        raise not_found
    clip = preview_clip_path(recording.artifact_dir, speaker_label, index)
    if clip is None:
        raise not_found
    return FileResponse(path=str(clip), media_type="audio/wav",
                        headers={"Cache-Control": "private, max-age=600"})


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
    limit: int = 20,
    offset: int = 0,
    threshold: float | None = None,
    recording_id: uuid.UUID | None = None,
    person: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    settings=Depends(get_settings_dep),
) -> SearchResultOut:
    """Search the transcript corpus (vts-uurt).

    Returns nothing when nothing clears the relevance threshold, rather than
    the nearest passages. The threshold is echoed back so an empty result can
    be read correctly.
    """
    try:
        hits, effective, total = await search_corpus(
            session, uuid.UUID(user.id), q, settings,
            threshold=threshold, limit=limit, offset=offset,
            recording_id=recording_id, person=person,
            created_from=created_from, created_to=created_to,
        )
    except ValueError as exc:
        # An unreachable offset is the caller's to fix, and the exception
        # already says how (narrow the query). Validating `offset` in the
        # signature instead would need the candidate ceiling duplicated here,
        # away from the code that enforces it. The MCP tool answers the same
        # refusal as a ToolError (vts-2c86).
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SearchResultOut(
        query=q,
        threshold=effective,
        total=total,
        returned=len(hits),
        offset=max(0, int(offset)),
        truncated=(max(0, int(offset)) + len(hits)) < total,
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


# ------------------------------------------------------------------ content
#
# The recording's content in the shapes the task routes have always served
# (vts-lzad). Both sets call the same responders in _helpers/recording_content,
# so a task route and its recording twin answer identically.


@router.get(
    "/api/recordings/{recording_id}/text/{variant}",
    responses={
        200: {
            "description": (
                "Text body. With Accept: application/json or ?offset/limit, a "
                "TextSliceOut JSON; with a Range header, 206 Partial Content."
            ),
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "text/markdown": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
            },
        },
        206: {"description": "Partial text (Range request)"},
        404: {"description": "Recording not found, or the text is not ready"},
    },
)
async def get_recording_text(
    recording_id: uuid.UUID,
    variant: TextVariant,
    request: Request,
    offset: int | None = None,
    limit: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    """A recording's raw transcript, processed transcript (`redacted`) or
    summary as text, sliceable like the task routes. Works after the
    originating task has been deleted."""
    recording = await recording_for_user_or_404(Repo(session), uuid.UUID(user.id), recording_id)
    return serve_recording_text(recording, variant, request=request, offset=offset, limit=limit)


@router.get("/api/recordings/{recording_id}/entries", include_in_schema=False)
async def get_recording_entries(
    recording_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> dict[str, Any]:
    """The player's two-level transcript (blocks with clickable sentences)."""
    recording = await recording_for_user_or_404(Repo(session), uuid.UUID(user.id), recording_id)
    return await recording_entries(recording, session)


@router.get(
    "/api/recordings/{recording_id}/subtitles",
    responses={
        200: {
            "description": "The transcript as a WebVTT track, speakers as voice tags.",
            "content": {"text/vtt": {"schema": {"type": "string"}}},
        },
        404: {"description": "Recording not found"},
    },
)
async def get_recording_subtitles(
    recording_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    """The transcript as WebVTT subtitles with resolved speaker names; a
    header-only track when there is no transcript."""
    recording = await recording_for_user_or_404(Repo(session), uuid.UUID(user.id), recording_id)
    return await recording_subtitles(recording, session)


@router.get(
    "/api/recordings/{recording_id}/results/{source}/{ref}",
    response_class=PlainTextResponse,
    responses={404: {"description": "Recording or result not found"}},
)
async def get_recording_result(
    recording_id: uuid.UUID,
    source: str,
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PlainTextResponse:
    """The text of one prompt result, e.g. `system/summary` or `user/<id>`,
    as listed in the recording's `prompt_results`."""
    recording = await recording_for_user_or_404(Repo(session), uuid.UUID(user.id), recording_id)
    return recording_result(recording, source, ref)

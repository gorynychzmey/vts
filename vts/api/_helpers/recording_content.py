"""Serving a recording's content over HTTP (vts-lzad).

The recording owns its content, so these responders are the one implementation
behind two sets of routes: `/api/recordings/{id}/...` and the task-addressed
routes that predate the split. A task route resolves its recording and calls
the same responder, which is what keeps the twins answering byte for byte the
same — two copies that merely agree today would not stay agreed.

`_serve_text` and its helpers moved here from `routers/artifacts.py`, which
imports them back for the task log.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.artifact_store import load_recording_player_blocks
from vts.api.schemas import TextSliceOut
from vts.db.models import Recording
from vts.db.repo import Repo
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    _artifact_path,
    read_recording_result,
)
from vts.services.subtitles import render_webvtt

TextVariant = Literal["raw", "redacted", "summary"]

_MAX_TEXT_SLICE_CHARS = 200_000  # safety cap for JSON-mode slice length


def _parse_range_header(value: str, total: int) -> tuple[int, int] | None:
    """Parse a `Range: bytes=START-END` header into (offset, length) char-pair.

    We use character offsets (not bytes) since the underlying artifacts are
    UTF-8 text and we want predictable slicing. Returns None for malformed
    or unsatisfiable ranges; the caller falls back to a full response.
    """
    if not value:
        return None
    value = value.strip().lower()
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):]
    if "," in spec:
        return None  # multipart ranges — not supported
    if "-" not in spec:
        return None
    start_str, end_str = spec.split("-", 1)
    # We deliberately do not support suffix-range (`bytes=-N`, "last N
    # bytes") — start_str must be present. Callers wanting the tail can
    # compute the offset from total_length.
    if not start_str:
        return None
    try:
        start = int(start_str)
        end = int(end_str) if end_str else total - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total:
        return None
    end = min(end, total - 1)
    return start, (end - start + 1)


def _serve_text(
    text: str,
    plain_media_type: str,
    *,
    request: Request,
    offset: int | None,
    limit: int | None,
) -> Response:
    """Serve a text artifact with three modes:

    1. Default (Accept: text/plain or */*; no slicing) → full body, original
       media-type (text/plain or text/markdown). Unchanged behaviour.
    2. Range header (`bytes=START-END`) → 206 Partial Content, plain text
       slice. Standard HTTP, works for curl/wget/anything HTTP-literate.
    3. Accept: application/json (+ optional ?offset/?limit) → JSON
       TextSliceOut with metadata. Works around 30KB client caps
       (notably ChatGPT Custom Actions). Slicing applied if requested,
       else full text wrapped in JSON.
    """
    total = len(text)

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range_header(range_header, total)
        if parsed is not None:
            start, length = parsed
            chunk = text[start:start + length]
            return Response(
                content=chunk,
                status_code=206,
                media_type=plain_media_type,
                headers={
                    "Content-Range": f"bytes {start}-{start + length - 1}/{total}",
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-cache",
                },
            )

    accept = (request.headers.get("accept") or "").lower()
    wants_json = "application/json" in accept and "text/plain" not in accept
    has_slice_query = offset is not None or limit is not None

    if wants_json or has_slice_query:
        off = max(0, offset or 0)
        if off > total:
            off = total
        lim = limit if limit is not None else _MAX_TEXT_SLICE_CHARS
        lim = max(0, min(lim, _MAX_TEXT_SLICE_CHARS))
        slice_text = text[off:off + lim]
        payload = TextSliceOut(
            text=slice_text,
            offset=off,
            length=len(slice_text),
            total_length=total,
            is_end=(off + len(slice_text)) >= total,
        )
        return JSONResponse(
            payload.model_dump(),
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
        )

    # Default: full plain text, as before.
    return Response(
        content=text,
        media_type=plain_media_type,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
    )


def _text_path_and_media_type(recording: Recording, variant: TextVariant) -> tuple[Path, str]:
    """Where a text variant lives and the media type it is served as, or 404
    with the message the task routes have always used."""
    if variant == "redacted":
        # The default name stands in for a NULL `redacted_path` (legacy rows):
        # a missing file reads as "not ready", as it always has.
        path = _artifact_path(recording, "redacted")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
        return path, "text/plain; charset=utf-8"
    # Raw and summary go by the stored path alone, never the default name: a
    # NULL summary_path means a regeneration is under way, and the previous
    # summary may still sit at outputs/summary.md — serving it would show a
    # summary the user has just asked to replace.
    stored = recording.transcript_path if variant == "raw" else recording.summary_path
    noun = "Transcript" if variant == "raw" else "Summary"
    if not stored:
        raise HTTPException(status_code=404, detail=f"{noun} is not ready")
    path = Path(stored)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{noun} file missing")
    if variant == "raw":
        media_type = "text/plain; charset=utf-8" if path.suffix == ".txt" else "application/json"
    else:
        media_type = (
            "text/markdown; charset=utf-8"
            if path.suffix in {".md", ".markdown"}
            else "application/json"
        )
    return path, media_type


def serve_recording_text(
    recording: Recording,
    variant: TextVariant,
    *,
    request: Request,
    offset: int | None,
    limit: int | None,
) -> Response:
    """One text variant of a recording, with the plain/JSON/Range negotiation."""
    path, media_type = _text_path_and_media_type(recording, variant)
    return _serve_text(
        path.read_text(encoding="utf-8"),
        media_type,
        request=request,
        offset=offset,
        limit=limit,
    )


async def recording_entries(recording: Recording, session: AsyncSession) -> dict[str, Any]:
    """The player's two-level transcript. Empty blocks (200) while there is no
    transcript, so a page can poll without special-casing not-ready."""
    return {"blocks": await load_recording_player_blocks(recording, session)}


async def recording_subtitles(recording: Recording, session: AsyncSession) -> Response:
    """The transcript as WebVTT, derived on demand so a speaker rename shows
    up at once; header-only while there is no transcript."""
    blocks = await load_recording_player_blocks(recording, session)
    return Response(content=render_webvtt(blocks), media_type="text/vtt; charset=utf-8")


def recording_result(recording: Recording, source: str, ref: str) -> PlainTextResponse:
    """One prompt result's text, read from the recording's own files."""
    try:
        return PlainTextResponse(read_recording_result(recording, source, ref))
    except RecordingArtifactMissing as exc:
        raise HTTPException(status_code=404, detail="Result not found") from exc


async def recording_for_task_or_404(
    repo: Repo, user_id: uuid.UUID, task_id: uuid.UUID
) -> Recording:
    """The recording of the user's task — the adapter step of every task
    content route. A task without one (a legacy row, or a race) is not ready."""
    task = await repo.get_task_for_user(user_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    recording = await repo.recording_for_task(task.id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Transcript is not ready")
    return recording


async def recording_for_user_or_404(
    repo: Repo, user_id: uuid.UUID, recording_id: uuid.UUID
) -> Recording:
    recording = await repo.get_recording_for_user(user_id, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return recording

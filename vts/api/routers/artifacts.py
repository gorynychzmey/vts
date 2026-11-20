"""Serving a task's produced artifacts: transcript, summary, log, media, player.

The content routes (transcript, summary, redacted, entries, subtitles) are
adapters since vts-lzad: the recording owns the content, so each resolves the
task's recording and calls the responder in `_helpers/recording_content.py`
that `/api/recordings/{id}/...` uses too. The task-only routes (log, media)
read the task's directory as before. `_serve_text` lives with the responders
and is imported back for the log.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.

No `tags=` on the router: `_install_custom_openapi()` in `vts.api.main`
derives the OpenAPI tag from the URL prefix, and an explicit tag overrides it.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.artifact_store import load_recording_player_blocks, shown_player_blocks
from vts.api._helpers.base import _find_media_file
from vts.api._helpers.recording_content import (  # noqa: F401 — re-exported
    _MAX_TEXT_SLICE_CHARS,
    _parse_range_header,
    _serve_text,
    recording_entries,
    recording_for_task_or_404,
    recording_subtitles,
    serve_recording_text,
)
from vts.api.deps import get_current_user, get_session_dep
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.media_kind import media_content_type, media_kind
from vts.services.recording_meta import recording_display_name

logger = logging.getLogger(__name__)

router = APIRouter()


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "_templates"


@lru_cache(maxsize=None)
def _template(name: str) -> str:
    """Read a player asset, cached for the process.

    Assets are read rather than embedded so the CSS and JS stay real files that
    an editor, a linter and `node --check` can all read. They ship inside the
    package, so this is a plain read, not user input — `name` is only ever a
    literal from this module.
    """
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


_PLAYER_MEDIA_UNAVAILABLE_MSG = {
    "en": "This media is no longer available.",
    "ru": "Медиа более не доступно.",
    "de": "Dieses Medium ist nicht mehr verfügbar.",
}

_PLAYER_AUTOSCROLL_MSG = {
    "en": "Autoscroll",
    "ru": "Автопрокрутка",
    "de": "Auto-Scrollen",
}


def _media_unavailable_block_html(*, transcript_only: bool = False) -> str:
    """The 'media is gone' state: a human-readable message (localized client-
    side) in place of the player. Marked with data-media-unavailable so the
    live SSE handler can swap the page into this state too. With
    `transcript_only` it also carries data-transcript-only, which tells
    player.js to render the transcript without a media element."""
    import json as _json

    msgs = _json.dumps(_PLAYER_MEDIA_UNAVAILABLE_MSG, ensure_ascii=False)
    default = _html.escape(_PLAYER_MEDIA_UNAVAILABLE_MSG["en"])
    marker = " data-transcript-only" if transcript_only else ""
    return (
        f'<p class="media-unavailable" data-media-unavailable{marker} '
        f"data-msgs='{msgs}'>{default}</p>"
    )


_PLAYER_PLACEHOLDER = re.compile(r"__(CSS|JS|MEDIA_BLOCK|AUTOSCROLL|TITLE|BLOCKS)__")


def _player_page_html(
    *,
    title: str,
    media_tag: str | None,
    blocks: list[dict[str, Any]],
    task_id: str | None = None,
    as_user: str | None = None,
    transcript_only: bool = False,
) -> str:
    """Self-contained /player page: the media element plus a two-level clickable
    transcript (vts-at8 / VOS-111, vts-u6w). The transcript keeps its block
    structure (an ASR segment for undiarized audio, a labelled speaker turn for
    diarized) and makes each SENTENCE inside a block clickable — a click seeks
    the player to that sentence's start.

    `media_tag` is a pre-built, already-escaped <video>/<audio> element, or
    None when the media file is gone (TTL / archive / delete) — then a
    localized 'media unavailable' message is shown in its place and the
    transcript is omitted (nothing to seek) — unless `transcript_only`: a
    recording whose task is gone has no media to play but still has its
    transcript, so the page keeps the blocks (a deep link marks its sentence).
    The blocks travel as JSON and player_core.js renders them, so the page and
    its live rebuild cannot disagree on numbering or on which sentences show.

    The shell, CSS and JS live in vts/api/_templates/ and are substituted here
    rather than being one f-string: as an f-string every CSS and JS brace had
    to be doubled (125 of them), which no editor or linter could check.
    """
    if media_tag is None:
        media_block = _media_unavailable_block_html(transcript_only=transcript_only)
        if not transcript_only:
            blocks = []
    else:
        media_block = media_tag
    # The transcript is rendered client-side by player_core.js, the one
    # renderer shared with the live rebuild and the MCP view; the page only
    # carries the blocks. Escaping <, > and & keeps a sentence like
    # "</script>" from closing the data element: JSON.parse turns the escapes
    # back into the original characters.
    blocks_json = (
        json.dumps(shown_player_blocks(blocks), ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    blocks_html = (
        f'<script type="application/json" id="vts-player-blocks">{blocks_json}</script>'
    )
    # Autoscroll checkbox: rendered whenever media is present, even if the
    # transcript hasn't arrived yet (task still processing -> blocks=[] on
    # first paint). The transcript streams in later via transcript_updated
    # + rebuildTranscript, which (re)wires the scroll listener (vts-eho).
    autoscroll_html = ""
    if media_tag is not None:
        ac_msgs = json.dumps(_PLAYER_AUTOSCROLL_MSG, ensure_ascii=False)
        autoscroll_html = (
            '<label class="autoscroll-toggle">'
            '<input type="checkbox" id="autoscroll-toggle" checked>'
            f"<span data-autoscroll-label data-msgs='{ac_msgs}'>"
            f"{_html.escape(_PLAYER_AUTOSCROLL_MSG['en'])}</span>"
            "</label>"
        )
    # Live logic is only wired when we know which task the page is for. The
    # page opens the shared SSE stream and reacts to this task's events:
    #   transcript_updated -> re-fetch /transcript-entries and rebuild the list
    #                         (covers first assembly AND rerender after resolve)
    #   task_status canceled/deleted -> swap into the media-unavailable state
    # Plus a <video>/<audio> error handler for media that vanishes mid-session.
    live_script = ""
    if task_id:
        live_script = (
            _template("player_live.js")
            .replace("__TASK_ID__", json.dumps(str(task_id)))
            .replace("__AS_USER__", json.dumps(as_user or ""))
            .replace(
                "__MEDIA_MSGS__",
                json.dumps(_PLAYER_MEDIA_UNAVAILABLE_MSG, ensure_ascii=False),
            )
        )

    js = _template("player_core.js") + "\n" + _template("player.js").replace(
        "/*__LIVE_SCRIPT__*/", live_script
    )
    # One pass over the shell: inserted text is never scanned again, so a
    # title or a sentence that happens to contain a placeholder name stays
    # literal. Chained .replace() calls could not promise that for both the
    # title and the transcript at once — whichever went first was rescanned.
    values = {
        "CSS": _template("player.css"),
        "JS": js,
        "MEDIA_BLOCK": media_block,
        "AUTOSCROLL": autoscroll_html,
        "TITLE": _html.escape(title),
        "BLOCKS": blocks_html,
    }
    return _PLAYER_PLACEHOLDER.sub(lambda m: values[m.group(1)], _template("player.html"))


@router.get(
    "/api/tasks/{task_id}/transcript",
    responses={
        200: {
            "description": (
                "Raw transcript. Default response is text/plain (full body). "
                "With Accept: application/json or ?offset/limit query, returns a "
                "TextSliceOut JSON. With a `Range: bytes=START-END` header, returns "
                "206 Partial Content. See docs/API.md for the rationale."
            ),
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
            },
        },
        206: {"description": "Partial transcript (Range request)"},
        404: {"description": "Task or transcript artifact not found"},
    },
)
async def get_transcript(
    task_id: uuid.UUID,
    request: Request,
    offset: int | None = None,
    limit: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    recording = await recording_for_task_or_404(Repo(session), uuid.UUID(user.id), task_id)
    return serve_recording_text(recording, "raw", request=request, offset=offset, limit=limit)


@router.get(
    "/api/tasks/{task_id}/transcript-entries",
    include_in_schema=False,
)
async def get_transcript_entries(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> dict[str, Any]:
    """Two-level transcript for the /player page: blocks (ASR segment /
    speaker turn) each carrying a resolved speaker `label` and a list of
    clickable `sentences` with their own timecodes (vts-at8, vts-u6w).
    Returns {"blocks": []} (200, not 404) when the transcript isn't ready
    yet, so the page can poll on transcript_updated without special-casing
    the not-ready state."""
    recording = await recording_for_task_or_404(Repo(session), uuid.UUID(user.id), task_id)
    return await recording_entries(recording, session)


@router.get(
    "/api/tasks/{task_id}/subtitles",
    responses={
        200: {
            "description": (
                "The transcript as a WebVTT subtitle track (vts-fkyq). WebVTT "
                "rather than SRT because it carries the speaker natively as a "
                "voice tag; undiarized transcripts simply get no tag."
            ),
            "content": {"text/vtt": {"schema": {"type": "string"}}},
        },
        404: {"description": "Task not found"},
    },
)
async def get_subtitles(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    """The transcript as a WebVTT subtitle track, derived from the same blocks
    the /player page uses. Returns a header-only track when the transcript is
    not ready yet."""
    # Derived on demand rather than persisted: speaker names live in the
    # registry and can be renamed, and deriving the track keeps it correct
    # after a rename instead of freezing a stale name into a file.
    #
    # Not-ready yields 200 with an empty track, not 404, so a caller can show an
    # empty subtitle view without special-casing it — matching how
    # /transcript-entries returns an empty block list.
    recording = await recording_for_task_or_404(Repo(session), uuid.UUID(user.id), task_id)
    return await recording_subtitles(recording, session)


@router.get(
    "/api/tasks/{task_id}/summary",
    responses={
        200: {
            "description": (
                "Markdown summary. Default response is text/markdown (full body). "
                "With Accept: application/json or ?offset/limit, returns TextSliceOut. "
                "With Range header, returns 206 Partial Content."
            ),
            "content": {
                "text/markdown": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
            },
        },
        206: {"description": "Partial summary (Range request)"},
        404: {"description": "Task or summary artifact not found"},
    },
)
async def get_summary(
    task_id: uuid.UUID,
    request: Request,
    offset: int | None = None,
    limit: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    recording = await recording_for_task_or_404(Repo(session), uuid.UUID(user.id), task_id)
    return serve_recording_text(recording, "summary", request=request, offset=offset, limit=limit)


@router.get(
    "/api/tasks/{task_id}/redacted",
    responses={
        200: {
            "description": (
                "Redacted plain-text transcript. Supports the same paginated "
                "modes as /transcript (Accept: application/json or Range header)."
            ),
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
            },
        },
        206: {"description": "Partial redacted transcript (Range request)"},
        404: {"description": "Task or redacted transcript not found"},
    },
)
async def get_redacted_transcript(
    task_id: uuid.UUID,
    request: Request,
    offset: int | None = None,
    limit: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    recording = await recording_for_task_or_404(Repo(session), uuid.UUID(user.id), task_id)
    return serve_recording_text(recording, "redacted", request=request, offset=offset, limit=limit)


@router.get(
    "/api/tasks/{task_id}/log",
    responses={
        200: {
            "description": (
                "Plain-text task log. Empty body if the task has no log yet. "
                "Supports the same paginated modes as /transcript."
            ),
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/TextSliceOut"}},
            },
        },
        206: {"description": "Partial log (Range request)"},
        404: {"description": "Task not found"},
    },
)
async def get_log(
    task_id: uuid.UUID,
    request: Request,
    offset: int | None = None,
    limit: int | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    path = Path(task.artifact_dir) / "logs" / "task.log"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return _serve_text(
        text,
        "text/plain; charset=utf-8",
        request=request,
        offset=offset,
        limit=limit,
    )


@router.get("/api/tasks/{task_id}/media")
async def get_media(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> FileResponse:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    media_file = _find_media_file(task.artifact_dir)
    if media_file is None:
        raise HTTPException(status_code=404, detail="Media file not available")
    return FileResponse(
        path=str(media_file),
        filename=media_file.name,
        media_type=media_content_type(media_file),
    )


@router.get("/player/{item_id}", include_in_schema=False, response_class=HTMLResponse)
async def media_player(
    item_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> HTMLResponse:
    """The player, addressed by a task id or a recording id (vts-lzad).

    The transcript comes from the recording; the media and the live wiring
    come from the task, because both are the task runtime's. A recording whose
    task is gone therefore gets a transcript-only page.
    """
    repo = Repo(session)
    user_id = uuid.UUID(user.id)
    # A task id first: deep links from before the split (VOS-134) name tasks.
    task = await repo.get_task_for_user(user_id, item_id)
    if task is not None:
        recording = await repo.recording_for_task(task.id)
    else:
        recording = await repo.get_recording_for_user(user_id, item_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if recording.source_task_id is not None:
            task = await repo.get_task_for_user(user_id, recording.source_task_id)
    blocks = (
        await load_recording_player_blocks(recording, session)
        if recording is not None
        else []
    )
    if task is None:
        # No task, no media and no task events to follow: the transcript alone,
        # which is still what a citation needs (?t= marks its sentence).
        html = _player_page_html(
            title=recording_display_name(recording.title, recording.source_url) or "player",
            media_tag=None,
            blocks=blocks,
            transcript_only=True,
        )
        return HTMLResponse(html)

    task_id = task.id
    media_file = _find_media_file(task.artifact_dir)
    # Propagate admin impersonation through the page: the media <src>, the
    # SSE entries re-fetch, all must resolve as the same acting user.
    acting_as = request.query_params.get("as_user")
    # The recording's name, as the library shows it. The last path segment of
    # source_url used to stand in for it, which put "mainEvent?simulive=y&…"
    # above a webinar while the recording had a proper title (vts-kb4z).
    title = recording_display_name(
        recording.title if recording is not None else task.source_title,
        task.source_url,
    ) or (media_file.name if media_file else "player")
    if media_file is None:
        # Media gone (TTL / archive / delete). Render a human-readable
        # "unavailable" page (200), not a raw 404 (vts-at8). No player,
        # no transcript — nothing to seek against. Still wires SSE so a
        # later task_status keeps the page in sync.
        html = _player_page_html(
            title=title,
            media_tag=None,
            blocks=[],
            task_id=str(task_id),
            as_user=acting_as,
        )
        return HTMLResponse(html)
    kind = media_kind(media_file)
    # <video>/<audio> fires its own request to /api/tasks/<id>/media, which
    # must resolve to the same acting user as the page itself — otherwise
    # the request resolves as the admin and ownership check returns 404.
    src = f"/api/tasks/{task_id}/media"
    if acting_as:
        src = f"{src}?{urlencode({'as_user': acting_as})}"
    tag = (
        f'<video controls autoplay src="{_html.escape(src, quote=True)}"></video>'
        if kind == "video"
        else f'<audio controls autoplay src="{_html.escape(src, quote=True)}"></audio>'
    )
    html = _player_page_html(
        title=title,
        media_tag=tag,
        blocks=blocks,
        task_id=str(task_id),
        as_user=acting_as,
    )
    return HTMLResponse(html)

"""Serving a task's produced artifacts: transcript, summary, log, media, player.

Read-only endpoints over what the pipeline wrote to a task's artifact
directory. Grouped together because they share `_serve_text()` — the
plain/JSON/Range content negotiation every text artifact uses.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged; `_serve_text`, `_parse_range_header` and
`_player_page_html` came along because nothing outside this module used them.

No `tags=` on the router: `_install_custom_openapi()` in `vts.api.main`
derives the OpenAPI tag from the URL prefix, and an explicit tag overrides it.
"""

from __future__ import annotations

import html as _html
import json
import logging
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.artifact_store import _load_player_blocks
from vts.api._helpers.base import _find_media_file
from vts.api.deps import get_current_user, get_session_dep
from vts.api.schemas import TextSliceOut
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.media_kind import media_content_type, media_kind

logger = logging.getLogger(__name__)

router = APIRouter()


_MAX_TEXT_SLICE_CHARS = 200_000  # safety cap for JSON-mode slice length


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


def _format_timecode(seconds: float) -> str:
    """Whole-second H:MM:SS / M:SS label for a transcript cue."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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


def _media_unavailable_block_html() -> str:
    """The 'media is gone' state: a human-readable message (localized client-
    side) in place of the player. Marked with data-media-unavailable so the
    live SSE handler can swap the page into this state too."""
    import json as _json

    msgs = _json.dumps(_PLAYER_MEDIA_UNAVAILABLE_MSG, ensure_ascii=False)
    default = _html.escape(_PLAYER_MEDIA_UNAVAILABLE_MSG["en"])
    return (
        f'<p class="media-unavailable" data-media-unavailable '
        f"data-msgs='{msgs}'>{default}</p>"
    )


def _player_block_html(block: dict[str, Any]) -> str:
    """One transcript block: its speaker label (when diarized) plus each inner
    sentence as an individually clickable cue that seeks to its own start.
    A block with a single sentence renders as one cue — same structure, so the
    whole block is still clickable when there were no finer timings."""
    label = str(block.get("label") or "").strip()
    label_html = (
        f'<div class="block-label">{_html.escape(label)}</div>' if label else ""
    )
    cues: list[str] = []
    for sentence in block.get("sentences") or []:
        try:
            start = float(sentence.get("start"))
        except (TypeError, ValueError):
            continue
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        cues.append(
            f'<span class="cue" data-start="{start}" role="button" tabindex="0" '
            f'title="{_format_timecode(start)}">{_html.escape(text)}</span>'
        )
    if not cues:
        return ""
    return f'<li class="block">{label_html}<p class="block-body">{" ".join(cues)}</p></li>'


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


def _player_page_html(
    *,
    title: str,
    media_tag: str | None,
    blocks: list[dict[str, Any]],
    task_id: str | None = None,
    as_user: str | None = None,
) -> str:
    """Self-contained /player page: the media element plus a two-level clickable
    transcript (vts-at8 / VOS-111, vts-u6w). The transcript keeps its block
    structure (an ASR segment for undiarized audio, a labelled speaker turn for
    diarized) and makes each SENTENCE inside a block clickable — a click seeks
    the player to that sentence's start.

    `media_tag` is a pre-built, already-escaped <video>/<audio> element, or
    None when the media file is gone (TTL / archive / delete) — then a
    localized 'media unavailable' message is shown in its place and the
    transcript is omitted (nothing to seek).
    Sentence/label text is escaped here; start times drive the seek via
    data-start.

    The shell, CSS and JS live in vts/api/_templates/ and are substituted here
    rather than being one f-string: as an f-string every CSS and JS brace had
    to be doubled (125 of them), which no editor or linter could check.
    """
    if media_tag is None:
        media_block = _media_unavailable_block_html()
        blocks = []
    else:
        media_block = media_tag
    rows = [html for block in blocks if (html := _player_block_html(block))]
    transcript_html = (
        f'<ol class="transcript">{"".join(rows)}</ol>' if rows else ""
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

    js = _template("player.js").replace("/*__LIVE_SCRIPT__*/", live_script)
    # Title is the only caller-supplied value in the shell, and it is escaped.
    # Substituted last, and the replacements above are ordered so that no
    # inserted text is itself scanned for a later placeholder.
    return (
        _template("player.html")
        .replace("__CSS__", _template("player.css"))
        .replace("__JS__", js)
        .replace("__MEDIA_BLOCK__", media_block)
        .replace("__AUTOSCROLL__", autoscroll_html)
        .replace("__TRANSCRIPT__", transcript_html)
        .replace("__TITLE__", _html.escape(title))
    )


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
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.transcript_path:
        raise HTTPException(status_code=404, detail="Transcript is not ready")
    path = Path(task.transcript_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Transcript file missing")
    plain_mt = "text/plain; charset=utf-8" if path.suffix == ".txt" else "application/json"
    return _serve_text(
        path.read_text(encoding="utf-8"),
        plain_mt,
        request=request,
        offset=offset,
        limit=limit,
    )


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
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"blocks": await _load_player_blocks(task, session)}


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
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.summary_path:
        raise HTTPException(status_code=404, detail="Summary is not ready")
    path = Path(task.summary_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Summary file missing")
    plain_mt = "text/markdown; charset=utf-8" if path.suffix in {".md", ".markdown"} else "application/json"
    return _serve_text(
        path.read_text(encoding="utf-8"),
        plain_mt,
        request=request,
        offset=offset,
        limit=limit,
    )


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
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
    return _serve_text(
        path.read_text(encoding="utf-8"),
        "text/plain; charset=utf-8",
        request=request,
        offset=offset,
        limit=limit,
    )


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


@router.get("/player/{task_id}", include_in_schema=False, response_class=HTMLResponse)
async def media_player(
    task_id: uuid.UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> HTMLResponse:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    media_file = _find_media_file(task.artifact_dir)
    # Propagate admin impersonation through the page: the media <src>, the
    # SSE entries re-fetch, all must resolve as the same acting user.
    acting_as = request.query_params.get("as_user")
    # source_url is "file://<name>" for uploads, an http URL otherwise;
    # in either case the last path segment is a sensible display name.
    title = (task.source_url or "").rsplit("/", 1)[-1] or (
        media_file.name if media_file else "player"
    )
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
    blocks = await _load_player_blocks(task, session)
    html = _player_page_html(
        title=title,
        media_tag=tag,
        blocks=blocks,
        task_id=str(task_id),
        as_user=acting_as,
    )
    return HTMLResponse(html)

"""MCP Apps: interactive views over the existing data (vts-vtma).

Tools stay primary. A host without MCP Apps support sees `show_recording` as
an ordinary tool with a short text answer, and ignores the `ui://` resource;
no existing tool changed its result for this.

* `ui://vts/player` — the player view: transcript and media.
* `ui://vts/search` — the view on a `search_transcripts` result; a passage
  opens the same player inside it. `search_transcripts` itself only gains
  `_meta.ui` (registered in search.py); its result is unchanged.
* `show_recording` — model-visible entry tool: "the user wants to look at this
  recording". Its result carries the first page of player blocks.
* `app_player_blocks` — app-only (`visibility: ["app"]`): further pages, for
  the view to fetch without passing a whole transcript through the model.
* `app_media_url` — app-only: a short-lived signed link to the media, which
  the view plays from the host's iframe (no cookie, no bearer there).

Spec: docs/superpowers/specs/2026-11-12-mcp-apps-design.md.
"""
from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.apps.config import UI_MIME_TYPE, AppConfig, ResourceCSP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult

from vts.api._helpers.artifact_store import (
    load_recording_player_blocks,
    shown_player_blocks,
)
from vts.core.config import get_settings
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.annotations import READ_ONLY
from vts.mcp.auth import mcp_authenticate
from vts.mcp.ui import player_view_html, search_view_html

PLAYER_URI = "ui://vts/player"
SEARCH_URI = "ui://vts/search"

# Blocks per page. A block is one ASR segment or speaker turn, so a two-hour
# recording is a handful of pages, and no single result carries the whole
# transcript — a host may show structuredContent to the model.
PAGE_BLOCKS = 100


def page_of_blocks(
    blocks: list[dict[str, Any]], cursor: str | None, size: int = PAGE_BLOCKS
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of blocks and the cursor for the next, None on the last page.

    The cursor is opaque to clients; it is the offset of the next block. A
    cursor that is not one this function produced is a caller mistake, said
    plainly rather than answered with an empty page that would read as "the
    transcript ends here".
    """
    start = 0
    if cursor:
        try:
            start = int(cursor)
        except ValueError:
            raise ToolError("invalid cursor") from None
        if start < 0 or start > len(blocks):
            raise ToolError("invalid cursor")
    end = start + size
    return blocks[start:end], (str(end) if end < len(blocks) else None)


def _summary_text(recording: Any, people: list[str], blocks: list[dict[str, Any]]) -> str:
    """The model-facing answer, and all a host without MCP Apps shows.

    Metadata only, no transcript text: the passage reading tools
    (`get_recording_transcript`) exist for that, with their own redaction
    choice, and the view shows the transcript to the user directly.
    """
    title = recording.title or "Untitled recording"
    parts = [f"Showing «{title}» in the VTS player."]
    if recording.duration_sec:
        minutes = round(recording.duration_sec / 60)
        parts.append(f"Duration about {minutes} min.")
    if people:
        parts.append("People: " + ", ".join(people) + ".")
    sentences = sum(len(b.get("sentences") or []) for b in blocks)
    if sentences:
        parts.append(f"{sentences} timed sentences.")
    else:
        parts.append("There is no transcript yet.")
    parts.append(
        f"To read or quote it, call get_recording_transcript with "
        f"recording_id={recording.id}."
    )
    return " ".join(parts)


def media_origin(public_base_url: str | None) -> str | None:
    """The origin media links point at: scheme and host of the public base
    URL, nothing else. None without one — then there is no media in the view,
    only the transcript (spec §5)."""
    if not public_base_url:
        return None
    parts = urlsplit(public_base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def player_app_config(public_base_url: str | None) -> AppConfig:
    """The view's CSP. No connectDomains: every byte of data goes through
    tools. resourceDomains is the media origin only — the host turns it into
    media-src, which is what lets <video> load a signed link."""
    origin = media_origin(public_base_url)
    csp = ResourceCSP(resource_domains=[origin]) if origin else None
    return AppConfig(prefers_border=True, csp=csp)


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        PLAYER_URI,
        name="vts_player",
        description="VTS player: a recording's transcript with timecodes and speakers.",
        mime_type=UI_MIME_TYPE,
        app=player_app_config(get_settings().public_base_url),
    )
    def _player_view() -> str:
        return player_view_html()

    @mcp.resource(
        SEARCH_URI,
        name="vts_search",
        description="VTS search results: passages that open the player on their moment.",
        mime_type=UI_MIME_TYPE,
        # Same CSP as the player: the view plays media once a passage is open.
        app=player_app_config(get_settings().public_base_url),
    )
    def _search_view() -> str:
        return search_view_html()

    async def _recording_blocks(recording_id: uuid.UUID) -> tuple[Any, list[dict[str, Any]], list[str]]:
        factory = get_db_session_factory()
        async with factory() as session:
            user, _settings = await mcp_authenticate(session)
            repo = Repo(session)
            recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
            blocks = await load_recording_player_blocks(recording, session)
            people: list[str] = []
            if recording.source_task_id is not None:
                names = await repo.speaker_names_for_tasks(
                    recording.user_id, [recording.source_task_id]
                )
                people = list(names.get(recording.source_task_id, []))
            return recording, blocks, people

    @mcp.tool(
        name="show_recording",
        annotations=READ_ONLY,
        app=AppConfig(resource_uri=PLAYER_URI),
    )
    async def _show_recording(
        recording_id: uuid.UUID,
        at_sec: float | None = None,
    ) -> ToolResult:
        """Show a recording to the user in the VTS player: its transcript with
        timecodes and speakers, which they can scroll and click.

        Use this when the user wants to SEE a recording or a moment in it —
        for example after `search_transcripts`, pass the hit's `recording_id`
        and its `start_sec` as `at_sec` to open the player on that passage.

        The text answer is a short summary, not the transcript. To read or
        quote the content yourself, use `get_recording_transcript`.
        """
        recording, blocks, people = await _recording_blocks(recording_id)
        shown = shown_player_blocks(blocks)
        first, next_cursor = page_of_blocks(shown, None)
        return ToolResult(
            content=_summary_text(recording, people, shown),
            structured_content={
                "recording_id": str(recording.id),
                "title": recording.title,
                "duration_sec": recording.duration_sec,
                "people": people,
                "at_sec": at_sec,
                "blocks": first,
                "next_cursor": next_cursor,
            },
        )

    @mcp.tool(
        name="app_player_blocks",
        annotations=READ_ONLY,
        app=AppConfig(visibility=["app"]),
    )
    async def _app_player_blocks(recording_id: uuid.UUID, cursor: str) -> ToolResult:
        """Next page of a recording's player blocks, for the VTS player view.
        Called by the view, not by the model."""
        _recording, blocks, _people = await _recording_blocks(recording_id)
        page, next_cursor = page_of_blocks(shown_player_blocks(blocks), cursor)
        return ToolResult(
            content=f"{len(page)} blocks",
            structured_content={"blocks": page, "next_cursor": next_cursor},
        )

    @mcp.tool(
        name="app_media_url",
        annotations=READ_ONLY,
        app=AppConfig(visibility=["app"]),
    )
    async def _app_media_url(recording_id: uuid.UUID) -> ToolResult:
        """A short-lived signed link to a recording's media, for the VTS player
        view. Called by the view, not by the model."""
        from vts.api._helpers.base import _find_media_file
        from vts.services.media_kind import media_kind
        from vts.services.media_urls import media_signing_key, signed_media_path

        factory = get_db_session_factory()
        async with factory() as session:
            user, settings = await mcp_authenticate(session)
            recording = await Repo(session).get_recording_for_user(
                uuid.UUID(user.id), recording_id
            )
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
        origin = media_origin(settings.public_base_url)
        media_file = _find_media_file(recording.artifact_dir)
        if origin is None or media_file is None:
            # Not an error: the view shows the transcript without a player.
            reason = "no public address configured" if origin is None else "no media"
            return ToolResult(
                content=f"No media: {reason}.",
                structured_content={"url": None, "kind": None, "expires_at": None, "reason": reason},
            )
        path, exp = signed_media_path(
            media_signing_key(settings), recording.id, uuid.UUID(user.id)
        )
        return ToolResult(
            content="Signed media link issued.",
            structured_content={
                "url": origin + path,
                "kind": media_kind(media_file),
                "expires_at": exp,
                "reason": None,
            },
        )

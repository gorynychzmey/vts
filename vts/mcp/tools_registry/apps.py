"""MCP Apps: interactive views over the existing data (vts-vtma).

Tools stay primary. A host without MCP Apps support sees `show_recording` as
an ordinary tool with a short text answer, and ignores the `ui://` resource;
no existing tool changed its result for this.

* `ui://vts/player` — the player view (transcript now, media in a later step).
* `show_recording` — model-visible entry tool: "the user wants to look at this
  recording". Its result carries the first page of player blocks.
* `app_player_blocks` — app-only (`visibility: ["app"]`): further pages, for
  the view to fetch without passing a whole transcript through the model.

Spec: docs/superpowers/specs/2026-11-12-mcp-apps-design.md.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.apps.config import UI_MIME_TYPE, AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult

from vts.api._helpers.artifact_store import (
    load_recording_player_blocks,
    shown_player_blocks,
)
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.annotations import READ_ONLY
from vts.mcp.auth import mcp_authenticate
from vts.mcp.ui import player_view_html

PLAYER_URI = "ui://vts/player"

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


def register(mcp: FastMCP) -> None:
    @mcp.resource(
        PLAYER_URI,
        name="vts_player",
        description="VTS player: a recording's transcript with timecodes and speakers.",
        mime_type=UI_MIME_TYPE,
        # No connectDomains: every byte of data goes through tools. Media,
        # and with it resourceDomains, comes in a later step.
        app=AppConfig(prefers_border=True),
    )
    def _player_view() -> str:
        return player_view_html()

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

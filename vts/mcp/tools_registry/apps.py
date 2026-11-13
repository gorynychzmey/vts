"""MCP Apps: interactive views over the existing data (vts-vtma).

Tools stay primary. A host without MCP Apps support sees `show_recording` as
an ordinary tool with a short text answer, and ignores the `ui://` resource;
no existing tool changed its result for this.

* `ui://vts/player` — the player view: transcript and media.
* `ui://vts/search` — the view on a `search_transcripts` result; a passage
  opens the same player inside it. `search_transcripts` itself only gains
  `_meta.ui` (registered in search.py); its result is unchanged.
* `ui://vts/summary` + `show_summary` — a recording's summary and its other
  prompt results, switchable in the view.
* `ui://vts/speakers` + `show_speakers` — who each voice of a recording is:
  listen to a voice, bind it to a person, create one, leave it anonymous or
  mark it noise. Saved by `app_resolve_speakers` through the same service as
  the web UI's speaker dialog; clips play through `app_speaker_preview_url`.
* `regenerate_summary` — re-run a recording's prompts with a chosen set, all
  at once, as the web UI's "restart the final summary" does. The previous
  results are cleared first, so it is marked destructive and the view asks
  before calling it.
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
from vts.mcp.annotations import READ_ONLY, RERUN_PROMPTS, UPDATE
from vts.mcp.auth import mcp_authenticate
from vts.mcp.ui import player_view_html, search_view_html, speakers_view_html, summary_view_html

PLAYER_URI = "ui://vts/player"
SEARCH_URI = "ui://vts/search"
SUMMARY_URI = "ui://vts/summary"
SPEAKERS_URI = "ui://vts/speakers"

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


def prompt_results_index(recording: Any) -> list[dict[str, str]]:
    """The prompt results a recording has, as [{ref, name}], for the view's
    switcher. Only completed ones: a pending entry has no text to show. The
    summary file of an old recording (before results were snapshotted into
    meta) counts as "system:summary"."""
    from pathlib import Path

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in (getattr(recording, "meta", None) or {}).get("prompt_results") or []:
        if not isinstance(entry, dict) or entry.get("status") not in (None, "completed"):
            continue
        ref = f"{entry.get('source', '')}:{entry.get('id', '')}"
        if ref in seen or ref == ":":
            continue
        seen.add(ref)
        out.append({"ref": ref, "name": str(entry.get("name") or ref)})
    summary = getattr(recording, "summary_path", None)
    if "system:summary" not in seen and summary and Path(summary).exists():
        out.insert(0, {"ref": "system:summary", "name": "Summary"})
    return out


_MATCH_OUTCOMES = ("auto", "grey", "miss")


def voice_options(match: dict[str, Any], people: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The people a voice can be bound to, in the web dialog's order: the
    matcher's candidates by distance, then everyone else by name (no distance
    to compare — not "infinitely far")."""
    candidates = [c for c in match.get("candidates") or [] if isinstance(c, dict)]
    ranked = sorted(candidates, key=lambda c: float(c.get("distance") or 0))
    ids = {str(c.get("speaker_id")) for c in ranked}
    rest = sorted((p for p in people if str(p["id"]) not in ids), key=lambda p: str(p["name"]))
    return [
        {"speaker_id": str(c.get("speaker_id")), "name": c.get("name"), "distance": c.get("distance")}
        for c in ranked
    ] + [{"speaker_id": str(p["id"]), "name": p["name"], "distance": None} for p in rest]


def resolution_for_choice(
    label: str, match: dict[str, Any], choice: dict[str, Any], people: list[dict[str, str]]
) -> dict[str, Any]:
    """Turn a view's plain choice for one voice into the resolution the save
    service takes — the same mapping as the web dialog's buildResolutions /
    resolveOutcomeCode (vts/static/app.js), so the calibration data stays
    meaningful whichever UI saved it.

    choice: {"kind": "person", "speaker_id"} | {"kind": "new", "new_name"} |
            {"kind": "anonymous"}, plus optional "is_noise", "add_fragment".
    """
    outcome = match.get("outcome") if match.get("outcome") in _MATCH_OUTCOMES else "miss"
    options = voice_options(match, people)
    matched = str(match["speaker_id"]) if match.get("speaker_id") else None
    kind = choice.get("kind")
    selection = str(choice.get("speaker_id") or "") if kind == "person" else None
    if kind == "person" and not selection:
        raise ToolError(f"{label}: a person needs speaker_id")
    if kind not in ("person", "new", "anonymous"):
        raise ToolError(f"{label}: unknown choice {kind!r}")
    bound = kind == "person"
    if outcome == "miss":
        code = "manual_match" if bound else "left_anonymous"
    elif outcome == "auto":
        code = "auto_accepted" if bound and selection == matched else "auto_overridden"
    else:  # grey
        if not bound:
            code = "left_anonymous"
        else:
            code = "confirmed" if options and selection == options[0]["speaker_id"] else "rejected"
    picked = next((o for o in options if o["speaker_id"] == selection), None)
    distance = picked["distance"] if picked and isinstance(picked["distance"], (int, float)) else (
        match.get("distance") if isinstance(match.get("distance"), (int, float)) else None
    )
    base = {
        "speaker_label": label,
        "outcome": code,
        "distance": distance,
        "is_noise": bool(choice.get("is_noise")),
    }
    add_fragment = bool(choice.get("add_fragment", outcome != "miss"))
    if kind == "new":
        name = str(choice.get("new_name") or "").strip()
        if name:
            return {**base, "action": "bind_new", "new_name": name, "add_fragment": True}
        return {**base, "action": "leave_anonymous", "add_fragment": False}
    if kind == "anonymous":
        return {**base, "action": "leave_anonymous", "add_fragment": False}
    if outcome == "auto" and selection == matched:
        return {**base, "action": "accept_auto", "speaker_id": selection, "add_fragment": add_fragment}
    return {**base, "action": "bind_existing", "speaker_id": selection, "add_fragment": add_fragment}


# How often the summary view asks how a regeneration is going.
REGENERATE_POLL_SEC = 5


def regenerate_state(task: Any) -> dict[str, Any]:
    """Whether the view may offer to re-run the prompts, and with which set
    preselected. A recording whose task was deleted cannot re-run; a task
    still being processed cannot yet."""
    from vts.api._helpers.serialization import can_restart_final_summary_task

    if task is None:
        return {"available": False, "reason": "task_deleted", "current": [], "poll_sec": REGENERATE_POLL_SEC}
    current = [
        f"{p.get('source', '')}:{p.get('id', '')}"
        for p in (task.options or {}).get("prompts") or []
        if isinstance(p, dict)
    ]
    available = can_restart_final_summary_task(task)
    return {
        "available": available,
        "reason": None if available else f"task_{task.status.value}",
        "current": current,
        "poll_sec": REGENERATE_POLL_SEC,
    }


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

    @mcp.resource(
        SUMMARY_URI,
        name="vts_summary",
        description="VTS summary: a recording's summary and other prompt results.",
        mime_type=UI_MIME_TYPE,
        app=AppConfig(prefers_border=True),
    )
    def _summary_view() -> str:
        return summary_view_html()

    @mcp.resource(
        SPEAKERS_URI,
        name="vts_speakers",
        description="VTS speakers: who each voice of a recording is.",
        mime_type=UI_MIME_TYPE,
        # Plays voice clips from the public origin, like the player's media.
        app=player_app_config(get_settings().public_base_url),
    )
    def _speakers_view() -> str:
        return speakers_view_html()

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

    @mcp.tool(
        name="show_summary",
        annotations=READ_ONLY,
        app=AppConfig(resource_uri=SUMMARY_URI),
    )
    async def _show_summary(recording_id: uuid.UUID, ref: str = "system:summary") -> ToolResult:
        """Show a recording's summary to the user in the VTS summary view,
        with a switcher to the recording's other prompt results (memos, custom
        prompts).

        `ref` picks the result shown first: "system:summary" (default) or
        "user:<prompt-uuid>". The text answer names the recording and the
        results it has, not their text; to read or quote a result yourself,
        use `get_recording_prompt_result`.
        """
        from vts.mcp.tools_registry.recordings import recording_prompt_result

        factory = get_db_session_factory()
        async with factory() as session:
            user, _settings = await mcp_authenticate(session)
            repo = Repo(session)
            recording = await repo.get_recording_for_user(uuid.UUID(user.id), recording_id)
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
            task = None
            if recording.source_task_id is not None:
                task = await repo.get_task_for_user(uuid.UUID(user.id), recording.source_task_id)
        regenerate = regenerate_state(task)
        index = prompt_results_index(recording)
        result = recording_prompt_result(recording, ref)
        wanted = f"{result.source}:{result.id}"
        title = recording.title or "Untitled recording"
        names = ", ".join(r["name"] for r in index) or "none"
        return ToolResult(
            content=(
                f"Showing «{title}» ({wanted}) in the VTS summary view. "
                f"Results available: {names}. To read or quote one, call "
                f"get_recording_prompt_result with recording_id={recording.id}."
            ),
            structured_content={
                "recording_id": str(recording.id),
                "title": recording.title,
                "ref": wanted,
                "markdown": result.content,
                "results": index,
                "regenerate": regenerate,
            },
        )

    @mcp.tool(name="regenerate_summary", annotations=RERUN_PROMPTS)
    async def _regenerate_summary(recording_id: uuid.UUID, prompts: list[str]) -> ToolResult:
        """Re-run a recording's prompts (summary, memos, custom prompts) with
        the set given, all at once — the same as "restart the final summary"
        in the VTS web UI.

        `prompts` are refs: "system:summary", "user:<prompt-uuid>" (see
        `list_prompts`). ALL current prompt results of the recording are
        cleared before the new ones are produced, so ask the user first. The
        work is queued and takes minutes; follow it with `get_status` on the
        returned `task_id`, then read the results with
        `get_recording_prompt_result` or `show_summary`.

        Needs the task that produced the recording; a recording whose task was
        deleted keeps its results but cannot re-run them.
        """
        from vts.services.prompt_registry import parse_ref, system_prompt_keys
        from vts.services.redis_bus import RedisBus
        from vts.services.redis_client import make_redis
        from vts.services.summary_restart import (
            SummaryRestartRefused,
            restart_final_with_prompts,
        )

        refs: list[dict[str, str]] = []
        for raw in prompts or []:
            try:
                source, ident = parse_ref(raw)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            ref = {"source": source, "id": ident}
            if ref not in refs:
                refs.append(ref)
        if not refs:
            raise ToolError("prompts must name at least one prompt")

        factory = get_db_session_factory()
        async with factory() as session:
            user, settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            repo = Repo(session)
            recording = await repo.get_recording_for_user(user_id, recording_id)
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
            if recording.source_task_id is None:
                raise ToolError(
                    "The task that produced this recording was deleted; its "
                    "prompt results remain, but they cannot be re-run."
                )
            for ref in refs:
                if ref["source"] == "system" and ref["id"] not in system_prompt_keys():
                    raise ToolError(f"unknown system prompt: {ref['id']}")
                if ref["source"] == "user":
                    try:
                        prompt_id = uuid.UUID(ref["id"])
                    except ValueError:
                        raise ToolError(f"invalid prompt id: {ref['id']}") from None
                    if await repo.get_prompt(user_id, prompt_id) is None:
                        raise ToolError(f"unknown prompt: user:{ref['id']}")
            # Locked: two regenerate clicks must run one after the other, not
            # interleave their clear-and-rebuild of the same task.
            task = await repo.get_task_for_user(user_id, recording.source_task_id, lock=True)
            if task is None:
                raise ToolError(
                    "The task that produced this recording was deleted; its "
                    "prompt results remain, but they cannot be re-run."
                )
            redis = make_redis(settings.redis_url)
            try:
                status = await restart_final_with_prompts(
                    repo, task, refs, RedisBus(redis, settings)
                )
            except SummaryRestartRefused:
                raise ToolError(
                    f"The task is {task.status.value}; its prompts can be re-run "
                    "once it has finished."
                ) from None
            finally:
                await redis.aclose()
        names = ", ".join(f"{r['source']}:{r['id']}" for r in refs)
        return ToolResult(
            content=(
                f"Re-running {len(refs)} prompt(s) ({names}) for «{recording.title or 'Untitled recording'}». "
                f"The previous results were cleared; new ones appear when task "
                f"{task.id} finishes — follow it with get_status."
            ),
            structured_content={
                "task_id": str(task.id),
                "status": status,
                "prompts": [f"{r['source']}:{r['id']}" for r in refs],
            },
        )

    async def _speakers_context(session: Any, user_id: uuid.UUID, recording_id: uuid.UUID):
        """The recording, its task (or None), the enriched matches (or None)
        and the user's people."""
        from vts.services.speaker_resolution import speaker_matches_view

        repo = Repo(session)
        recording = await repo.get_recording_for_user(user_id, recording_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        task = None
        if recording.source_task_id is not None:
            task = await repo.get_task_for_user(user_id, recording.source_task_id)
        matches = await speaker_matches_view(repo, user_id, task) if task is not None else None
        people = [{"id": str(sp.id), "name": sp.name} for sp in await repo.list_speakers(user_id)]
        return recording, task, matches, people

    @mcp.tool(
        name="show_speakers",
        annotations=READ_ONLY,
        app=AppConfig(resource_uri=SPEAKERS_URI),
    )
    async def _show_speakers(recording_id: uuid.UUID) -> ToolResult:
        """Show the voices of a recording to the user in the VTS speakers view,
        where they can listen to each voice and say who it is — bind it to a
        person, create a new one, leave it anonymous or mark it noise.

        The text answer lists the voices and who they are bound to now. The
        view saves through the same code as the VTS web UI's speaker dialog.
        """
        from vts.api._helpers.serialization import can_resolve_speakers_task
        from vts.services.speaker_resolution import preview_counts

        factory = get_db_session_factory()
        async with factory() as session:
            user, _settings = await mcp_authenticate(session)
            recording, task, matches, people = await _speakers_context(
                session, uuid.UUID(user.id), recording_id
            )
        title = recording.title or "Untitled recording"
        if task is None or matches is None:
            reason = "task_deleted" if task is None else "not_diarized"
            return ToolResult(
                content=(
                    f"«{title}» has no voices to show: "
                    + ("the task that produced it was deleted." if task is None
                       else "it was not processed with speaker separation.")
                ),
                structured_content={"recording_id": str(recording.id), "title": recording.title,
                                    "voices": [], "people": people, "editable": False,
                                    "reason": reason, "awaiting_input": False},
            )
        counts = preview_counts(task.artifact_dir)
        voices = []
        # In the matcher's order (SPEAKER_00, 01, ...), as the web dialog lists them.
        for label, match in matches.items():
            if not isinstance(match, dict):
                continue
            voices.append({
                "label": label,
                "display_label": match.get("display_label") or label,
                "outcome": match.get("outcome") if match.get("outcome") in _MATCH_OUTCOMES else "miss",
                "matched_speaker_id": str(match["speaker_id"]) if match.get("speaker_id") else None,
                "decided_speaker_id": match.get("decided_speaker_id"),
                "decided_name": match.get("decided_name"),
                "decided_is_noise": match.get("decided_is_noise"),
                "options": voice_options(match, people),
                "clips": counts.get(label, 0),
            })
        editable = can_resolve_speakers_task(task)
        awaiting = getattr(task.status, "value", str(task.status)) == "awaiting_input"
        lines = []
        for v in voices:
            who = "noise" if v["decided_is_noise"] else (v["decided_name"] or "not identified")
            lines.append(f"{v['display_label']}: {who}")
        return ToolResult(
            content=f"Showing the voices of «{title}» in the VTS speakers view. " + "; ".join(lines) + ".",
            structured_content={"recording_id": str(recording.id), "title": recording.title,
                                "voices": voices, "people": people, "editable": editable,
                                "reason": None if editable else f"task_{task.status.value}",
                                "awaiting_input": awaiting},
        )

    @mcp.tool(
        name="app_speaker_preview_url",
        annotations=READ_ONLY,
        app=AppConfig(visibility=["app"]),
    )
    async def _app_speaker_preview_url(recording_id: uuid.UUID, label: str, index: int = 0) -> ToolResult:
        """A short-lived signed link to one preview clip of a voice, for the
        VTS speakers view. Called by the view, not by the model."""
        from urllib.parse import quote

        from vts.services.media_urls import media_signing_key, signed_path
        from vts.services.speaker_resolution import preview_clip_path

        factory = get_db_session_factory()
        async with factory() as session:
            user, settings = await mcp_authenticate(session)
            recording = await Repo(session).get_recording_for_user(uuid.UUID(user.id), recording_id)
            if recording is None:
                raise HTTPException(status_code=404, detail="Recording not found")
        origin = media_origin(settings.public_base_url)
        if origin is None or preview_clip_path(recording.artifact_dir, label, index) is None:
            reason = "no public address configured" if origin is None else "no clip"
            return ToolResult(content=f"No clip: {reason}.",
                              structured_content={"url": None, "expires_at": None, "reason": reason})
        path = f"/api/recordings/{recording.id}/speaker-previews/{quote(label, safe='')}/{int(index)}/audio"
        signed, exp = signed_path(media_signing_key(settings), path, uuid.UUID(user.id))
        return ToolResult(content="Signed clip link issued.",
                          structured_content={"url": origin + signed, "expires_at": exp, "reason": None})

    @mcp.tool(
        name="app_resolve_speakers",
        annotations=UPDATE,
        app=AppConfig(visibility=["app"]),
    )
    async def _app_resolve_speakers(
        recording_id: uuid.UUID,
        voices: dict[str, dict[str, Any]],
        continue_task: bool = False,
    ) -> ToolResult:
        """Save who each voice of a recording is, from the VTS speakers view.
        `voices` maps a voice label to {"kind": "person"|"new"|"anonymous",
        "speaker_id"?, "new_name"?, "is_noise"?, "add_fragment"?}. Called by
        the view, not by the model."""
        from types import SimpleNamespace

        from vts.services.diarization import create_diarization_backend
        from vts.services.redis_client import make_redis
        from vts.services.speaker_resolution import resolve_task_speakers

        if not voices:
            raise ToolError("nothing to save")
        factory = get_db_session_factory()
        async with factory() as session:
            user, settings = await mcp_authenticate(session)
            user_id = uuid.UUID(user.id)
            recording, task, matches, people = await _speakers_context(session, user_id, recording_id)
            if task is None:
                raise ToolError("The task that produced this recording was deleted; its voices cannot be changed.")
            if matches is None:
                raise ToolError("This recording was not processed with speaker separation.")
            resolutions = []
            for label, choice in voices.items():
                if label not in matches or not isinstance(matches[label], dict):
                    raise ToolError(f"unknown voice: {label}")
                # The fields the save service reads, with the web request's
                # defaults for the ones a choice does not set.
                fields = {"speaker_id": None, "new_name": None, "voice_sample_id": None}
                fields.update(resolution_for_choice(label, matches[label], choice or {}, people))
                resolutions.append(SimpleNamespace(**fields))
            redis = make_redis(settings.redis_url)
            try:
                results = await resolve_task_speakers(
                    session,
                    user_id=user_id,
                    task_id=task.id,
                    resolutions=resolutions,
                    continue_task=bool(continue_task),
                    redis=redis,
                    settings=settings,
                    diarization=create_diarization_backend(
                        settings.diarization_url, settings.diarization_backend
                    ),
                )
            finally:
                await redis.aclose()
        return ToolResult(
            content=f"Saved {len(results)} voice(s).",
            structured_content={"results": results, "continued": bool(continue_task)},
        )


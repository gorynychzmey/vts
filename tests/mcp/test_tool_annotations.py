"""Every MCP tool declares whether it reads or writes (vts-ann1).

Clients group tools by `readOnlyHint` — that is how a user sees "these only
look, those change things" instead of one flat list of 29 names. Without the
hints every tool lands in the same bucket, and an assistant has no way to know
that `delete_prompt` is not like `list_prompts`.

The tests below are deliberately exhaustive rather than a spot check: a tool
added later with no annotation is the dangerous case, because a missing hint
reads as "unknown", and some clients treat unknown as safe.
"""
from __future__ import annotations

import pytest

# Everything that only reads. If a tool here ever starts writing, its entry
# must move — that is the point of listing them by name rather than by prefix.
READ_ONLY = {
    "list_tasks", "get_status", "get_transcript", "get_prompt_result",
    "wait_for_task",
    "list_prompts", "list_presets", "get_default_preset",
    "list_delivery_targets", "list_delivery_credentials", "get_delivery_status",
    "search_transcripts", "list_recordings",
    "list_people", "get_recording_transcript",
    "get_recording_prompt_result",
}

# Writes that a user would not want to lose without being asked.
DESTRUCTIVE = {
    # Deletes a task WITH its recording, files and transcript text.
    "delete_task",
    "delete_prompt", "delete_preset",
    "delete_delivery_credential", "delete_delivery_target",
}

# Writes that create or update. Not destructive: nothing the user had is lost.
# submit_video and retry_delivery sit here by the owner's decision — starting a
# run is cancellable, and a delivery retry repeats something already configured.
MUTATING = {
    "submit_video",
    "create_prompt", "update_prompt",
    "create_preset", "update_preset", "set_default_preset",
    "create_delivery_credential", "update_delivery_credential",
    "create_delivery_target", "update_delivery_target",
    "retry_delivery",
    "rename_recording",
}


async def _tools():
    from vts.mcp.server import build_mcp_server

    return {t.name: t for t in await build_mcp_server().list_tools()}


async def test_every_tool_is_classified() -> None:
    """The lists above must cover the server exactly.

    A tool missing from all three is one nobody decided about; a name in a list
    that no longer exists is a stale rule pretending to protect something.
    """
    names = set(await _tools())
    classified = READ_ONLY | DESTRUCTIVE | MUTATING
    assert not (names - classified), (
        f"tools with no read/write classification: {sorted(names - classified)}"
    )
    assert not (classified - names), (
        f"classified names that no longer exist: {sorted(classified - names)}"
    )


async def test_every_tool_carries_annotations() -> None:
    tools = await _tools()
    missing = [n for n, t in tools.items() if t.annotations is None]
    assert not missing, f"tools without annotations: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(READ_ONLY))
async def test_read_only_tools_say_so(name: str) -> None:
    tools = await _tools()
    ann = tools[name].annotations
    assert ann.readOnlyHint is True, f"{name} does not declare itself read-only"
    # A read cannot be destructive; saying both would be incoherent.
    assert not ann.destructiveHint, f"{name} is read-only but flagged destructive"


@pytest.mark.parametrize("name", sorted(DESTRUCTIVE))
async def test_destructive_tools_say_so(name: str) -> None:
    tools = await _tools()
    ann = tools[name].annotations
    assert ann.readOnlyHint is False, f"{name} claims to be read-only"
    assert ann.destructiveHint is True, (
        f"{name} deletes something but is not flagged destructive — a client "
        f"would call it without asking"
    )


@pytest.mark.parametrize("name", sorted(MUTATING))
async def test_mutating_tools_are_writes_but_not_destructive(name: str) -> None:
    tools = await _tools()
    ann = tools[name].annotations
    assert ann.readOnlyHint is False, f"{name} writes but claims to be read-only"
    assert ann.destructiveHint is False, (
        f"{name} is flagged destructive; that prompts the user for a call that "
        f"loses nothing"
    )

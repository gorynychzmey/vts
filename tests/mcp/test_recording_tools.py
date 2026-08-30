"""MCP tools addressed by recording, not by task (vts-lib3).

The task tools came first, when a task WAS the recording. After the split
`search_transcripts` returns `recording_id` as the identifier to keep — and
there was nothing to fetch with it. An assistant that found a passage had to
hop back to a task that may no longer exist.

These tests pin the shape of the fix: the tools exist, and their descriptions
point a client at recordings for artifacts, since a description is the only
thing an LLM client reads before choosing.
"""
from __future__ import annotations


async def _tools():
    from vts.mcp.server import build_mcp_server

    return {t.name: t for t in await build_mcp_server().list_tools()}


async def test_the_recording_tools_are_registered() -> None:
    tools = await _tools()
    assert "list_recordings" in tools
    assert "get_recording_transcript" in tools


async def test_the_transcript_tool_says_it_survives_a_deleted_task() -> None:
    """That property is the reason to prefer it, so it has to be stated.

    A client choosing between get_transcript(task_id) and
    get_recording_transcript(recording_id) has only the descriptions to go on.
    """
    tools = await _tools()
    description = (tools["get_recording_transcript"].description or "").lower()
    assert "search_transcripts" in description, (
        "the tool does not say it is the follow-up to a search"
    )
    assert "deleted" in description, (
        "the tool does not mention that it works when the task is gone"
    )


async def test_the_transcript_tool_offers_a_window_around_a_moment() -> None:
    # Fetching a two-hour transcript to show one quote buries the part that
    # mattered; the description has to point that out or nobody will use it.
    tools = await _tools()
    description = (tools["get_recording_transcript"].description or "").lower()
    assert "around_sec" in description
    assert "structured" in description


async def test_the_task_tools_are_still_there() -> None:
    # Kept deliberately: they answer questions about a RUN, and removing them
    # would break clients already wired to them.
    tools = await _tools()
    for name in ("get_transcript", "get_status", "list_tasks"):
        assert name in tools


# ---------------------------------------------------- links a client can follow

def test_a_hit_offers_both_links_with_their_different_lifetimes():
    """Two links, and the difference between them stated.

    Both are useful: the player opens the moment in the media for a person to
    watch, and the transcript reads the passage for the assistant. But they do
    not last equally — /player/{task} dies with the task, while the recording
    link does not. A client handed one URL cannot know which kind it holds, so
    both are returned, and the player one is simply absent when it would 404.
    """
    from vts.mcp.tools_registry.search import hit_links

    live = hit_links(
        recording_id="11111111-1111-1111-1111-111111111111",
        source_task_id="22222222-2222-2222-2222-222222222222",
        start_sec=754.0,
        base_url="https://vts.example",
    )
    assert live["transcript_url"] == (
        "https://vts.example/api/recordings/11111111-1111-1111-1111-111111111111"
        "/transcript?around_sec=754&window_sec=60"
    )
    assert live["player_url"] == (
        "https://vts.example/player/22222222-2222-2222-2222-222222222222?t=754"
    )


def test_a_hit_from_a_deleted_task_keeps_the_transcript_link_only():
    from vts.mcp.tools_registry.search import hit_links

    orphan = hit_links(
        recording_id="11111111-1111-1111-1111-111111111111",
        source_task_id=None,
        start_sec=12.0,
        base_url="https://vts.example",
    )
    assert orphan["player_url"] is None, "a dead player link was offered"
    assert orphan["transcript_url"], "the passage became unreachable"


def test_links_are_relative_when_no_base_url_is_configured():
    # A deployment without VTS_PUBLIC_BASE_URL still gets usable paths rather
    # than URLs pointing at a host that does not exist.
    from vts.mcp.tools_registry.search import hit_links

    links = hit_links(
        recording_id="11111111-1111-1111-1111-111111111111",
        source_task_id="22222222-2222-2222-2222-222222222222",
        start_sec=5.0,
        base_url=None,
    )
    assert links["player_url"].startswith("/player/")
    assert links["transcript_url"].startswith("/api/recordings/")

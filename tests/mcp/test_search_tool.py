"""The MCP search tool shares its rules with the HTTP endpoint (vts-uurt).

VOS-132 requires that the MCP search use EXACTLY the same threshold and
relevance rules as the UI/API. Two call sites that merely agree today would
drift, so the requirement is met structurally: both go through
`services.corpus_search.search_corpus`. These tests pin that, because it is the
kind of property a refactor breaks silently.
"""
from __future__ import annotations

import inspect


async def test_the_search_tool_is_registered() -> None:
    from vts.mcp.server import build_mcp_server

    tools = await build_mcp_server().list_tools()
    names = {tool.name for tool in tools}
    assert "search_transcripts" in names


async def test_the_tool_description_tells_a_client_what_empty_means() -> None:
    """An LLM client reads this description and decides what to do.

    An empty result must read as an answer ("the recordings do not cover this")
    rather than as a failure to work around — otherwise the obvious next move
    is to lower the threshold until something comes back, which recreates the
    behaviour the threshold exists to prevent.
    """
    from vts.mcp.server import build_mcp_server

    tools = await build_mcp_server().list_tools()
    tool = next(t for t in tools if t.name == "search_transcripts")
    description = (tool.description or "").lower()
    assert "empty" in description
    assert "do not lower" in description or "not lower" in description
    # And it must point at the identifier that survives task deletion.
    assert "recording_id" in description


def test_both_entry_points_call_the_same_search_function() -> None:
    """Structural, not behavioural — and that is the point.

    If the MCP tool ever grows its own query or its own default, the two
    surfaces can return different answers to the same question with no test
    failing anywhere. Asserting they share the function keeps that honest.
    """
    from vts.api.routers import recordings as api_module
    from vts.mcp.tools_registry import search as mcp_module
    from vts.services.corpus_search import search_corpus

    assert api_module.search_corpus is search_corpus
    assert mcp_module.search_corpus is search_corpus

    # Neither surface may hold its own threshold constant.
    for module in (api_module, mcp_module):
        source = inspect.getsource(module)
        assert "0.45" not in source, (
            f"{module.__name__} hard-codes a threshold instead of taking the "
            f"configured one"
        )


async def test_the_tool_explains_how_to_cite_a_hit() -> None:
    """A client that cannot build the link will paraphrase instead of citing.

    The player is addressed by TASK while the stable identifier is the
    RECORDING, so the URL shape is not guessable from the field names — it has
    to be stated. And a null source_task_id (deleted task) must be described,
    or a client will happily emit /player/None?t=12.
    """
    from vts.mcp.server import build_mcp_server

    tools = await build_mcp_server().list_tools()
    description = (next(
        t for t in tools if t.name == "search_transcripts").description or "")
    # Two links with different lifetimes, and the description has to say which
    # is which: a client told only about the player would produce dead
    # citations for exactly the recordings that outlived their jobs.
    assert "transcript_url" in description, "the durable link is not documented"
    assert "player_url" in description, "the media link is not documented"
    assert "get_recording_transcript" in description, (
        "the tool does not point at the recording-scoped follow-up"
    )
    assert "around_sec" in description
    assert "null" in description.lower(), (
        "the description does not say what a missing task means"
    )
    # And it must discourage assembling the player URL by hand, since only the
    # server knows whether the task still exists.
    assert "do not construct it yourself" in description.lower()

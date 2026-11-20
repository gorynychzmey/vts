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


# ------------------------------------------- the library shows state, not empties

async def test_mcp_list_recordings_reports_state_and_hides_empty(monkeypatch):
    """vts-lzad: a recording with no text is not listed; the rest carry state."""
    import uuid
    from contextlib import asynccontextmanager

    from fastmcp import Client
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import vts.mcp.server as server_mod
    import vts.mcp.tools_registry.recordings as recordings_mod
    from tests._db import ensure_pgvector, make_test_engine
    from tests.mcp.conftest import FakeUser
    from vts.core.config import Settings
    from vts.db.base import Base
    from vts.db.models import Recording, RecordingState, User, utcnow

    user_id = uuid.uuid4()
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(User(id=user_id, username="mcp-state"))
            await session.flush()
            with_text = Recording(
                id=uuid.uuid4(), user_id=user_id, title="Has text",
                artifact_dir="/tmp/vts-test/a",
                transcript_path="/tmp/vts-test/a/transcript.txt",
                listed_at=utcnow(),
                state=RecordingState.incomplete,
            )
            empty = Recording(
                id=uuid.uuid4(), user_id=user_id, title="Empty",
                artifact_dir="/tmp/vts-test/b",
            )
            session.add_all([with_text, empty])
            await session.commit()

            @asynccontextmanager
            async def _session_cm():
                yield session

            async def _authenticate(_session):
                return FakeUser(id=str(user_id)), Settings()

            monkeypatch.setattr(recordings_mod, "mcp_authenticate", _authenticate)
            monkeypatch.setattr(recordings_mod, "get_db_session_factory", lambda: _session_cm)

            async with Client(server_mod.build_mcp_server()) as client:
                result = await client.call_tool("list_recordings", {})

            assert result.is_error is False
            assert [(i.id, i.state) for i in result.data.items] == [
                (str(with_text.id), "incomplete")
            ]
            assert result.data.total == 1
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_list_recordings_offers_cursors_and_task_filter() -> None:
    """The library cursors and the task filter reach MCP clients (vts-lzad)."""
    tools = await _tools()
    props = tools["list_recordings"].parameters["properties"]
    for name in ("before_ts", "before_id", "after_ts", "after_id", "order", "task_ids"):
        assert name in props, f"list_recordings does not accept {name}"
    for name in ("limit", "offset"):
        assert name in props


async def test_list_recordings_rejects_half_cursor_pairs_and_bad_order() -> None:
    """Rejected before any database access, so no fixtures are needed."""
    import uuid

    from fastmcp import Client

    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        for args in (
            {"before_ts": "2026-01-01T00:00:00Z"},
            {"before_id": str(uuid.uuid4())},
            {"after_ts": "2026-01-01T00:00:00Z"},
            {"after_id": str(uuid.uuid4())},
            {"order": "sideways"},
        ):
            result = await client.call_tool(
                "list_recordings", args, raise_on_error=False
            )
            assert result.is_error is True, f"{args} was accepted"

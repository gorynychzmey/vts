"""The MCP Apps player view (vts-vtma.2).

Pins the contract a host relies on: the entry tool points at a `ui://`
resource that resolves, the resource is a self-contained HTML document of the
MCP Apps MIME type, the app-only tool is marked so the host keeps it from the
model, and the existing tools did not change.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from fastmcp import Client

from vts.core.config import Settings
from vts.mcp.tools_registry.apps import PAGE_BLOCKS, PLAYER_URI, page_of_blocks

_OWNER = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


@dataclass
class _User:
    id: str = str(_OWNER)
    username: str = "owner"


@dataclass
class _Recording:
    id: uuid.UUID
    user_id: uuid.UUID = _OWNER
    title: str | None = "Weekly call"
    duration_sec: float | None = 1800.0
    source_task_id: uuid.UUID | None = field(default_factory=uuid.uuid4)


class _Repo:
    def __init__(self, recordings):
        self.recordings = {r.id: r for r in recordings}

    async def get_recording_for_user(self, user_id, recording_id):
        r = self.recordings.get(recording_id)
        return r if r is not None and r.user_id == user_id else None

    async def speaker_names_for_tasks(self, user_id, task_ids):
        return {task_ids[0]: ["Вася"]}


def _blocks(n):
    return [
        {"label": f"Голос {i % 2 + 1}", "speaker": "SPEAKER_00", "start": i, "end": i + 1,
         "sentences": [{"start": float(i), "end": i + 1.0, "text": f"sentence {i}"}]}
        for i in range(n)
    ]


@asynccontextmanager
async def _session():
    yield object()


@pytest.fixture
def client_for(monkeypatch):
    import vts.mcp.tools_registry.apps as apps_mod
    from vts.mcp.server import build_mcp_server

    def make(recordings, blocks):
        async def _auth(_session):
            return _User(), Settings()

        async def _load(recording, _session):
            return blocks

        monkeypatch.setattr(apps_mod, "mcp_authenticate", _auth)
        monkeypatch.setattr(apps_mod, "get_db_session_factory", lambda: _session)
        monkeypatch.setattr(apps_mod, "Repo", lambda _s: _Repo(recordings))
        monkeypatch.setattr(apps_mod, "load_recording_player_blocks", _load)
        return Client(build_mcp_server())

    return make


# ------------------------------------------------------------ host contract

async def test_the_entry_tool_points_at_a_resource_that_resolves():
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
        uri = tools["show_recording"].meta["ui"]["resourceUri"]
        assert uri == PLAYER_URI
        contents = await client.read_resource(uri)
    assert contents[0].mimeType == "text/html;profile=mcp-app"


async def test_the_app_only_tool_is_marked_for_the_app_alone():
    # The host filters it from the model by this marker; listing it is what
    # the spec asks of the server.
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["app_player_blocks"].meta["ui"]["visibility"] == ["app"]
    assert "visibility" not in tools["show_recording"].meta["ui"]


async def test_the_server_announces_the_extension():
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        extensions = client.initialize_result.capabilities.model_dump().get("extensions") or {}
    assert "io.modelcontextprotocol/ui" in extensions


async def test_the_view_is_one_self_contained_document():
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        html = (await client.read_resource(PLAYER_URI))[0].text
    # Its scripts are inlined, the shared core included...
    assert "VtsPlayerCore" in html and "VtsAppBridge" in html
    assert "__CSS__" not in html and "__JS__" not in html
    # ...and nothing is fetched from elsewhere: the CSP declares no domains.
    assert not re.search(r'<(script|link|img)[^>]+(src|href)=', html)


async def test_existing_tools_carry_no_view():
    # Tools stay primary: the read tools a client already uses keep their
    # results and gain no UI.
    from vts.mcp.server import build_mcp_server

    async with Client(build_mcp_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("get_transcript", "get_recording_transcript", "search_transcripts"):
        assert "ui" not in (tools[name].meta or {}), name


# ------------------------------------------------------------ the entry tool

async def test_show_recording_returns_a_summary_and_the_first_page(client_for):
    rec = _Recording(id=uuid.uuid4())
    async with client_for([rec], _blocks(PAGE_BLOCKS + 5)) as client:
        result = await client.call_tool("show_recording", {"recording_id": str(rec.id), "at_sec": 12.5})
    data = result.structured_content
    assert data["recording_id"] == str(rec.id)
    assert data["title"] == "Weekly call"
    assert data["people"] == ["Вася"]
    assert data["at_sec"] == 12.5
    assert len(data["blocks"]) == PAGE_BLOCKS
    assert data["next_cursor"] == str(PAGE_BLOCKS)
    # Only what the player shows: no raw tag, no internal fields.
    assert data["blocks"][0] == {"label": "Голос 1", "sentences": [{"start": 0.0, "text": "sentence 0"}]}
    text = result.content[0].text
    assert "Weekly call" in text and "get_recording_transcript" in text
    # The model gets metadata, not the transcript.
    assert "sentence 0" not in text


async def test_a_short_transcript_fits_one_page(client_for):
    rec = _Recording(id=uuid.uuid4())
    async with client_for([rec], _blocks(3)) as client:
        data = (await client.call_tool("show_recording", {"recording_id": str(rec.id)})).structured_content
    assert len(data["blocks"]) == 3
    assert data["next_cursor"] is None
    assert data["at_sec"] is None


async def test_someone_elses_recording_is_not_shown(client_for):
    rec = _Recording(id=uuid.uuid4(), user_id=uuid.uuid4())
    async with client_for([rec], _blocks(3)) as client:
        result = await client.call_tool(
            "show_recording", {"recording_id": str(rec.id)}, raise_on_error=False
        )
    assert result.is_error


# ------------------------------------------------------------ the app-only tool

async def test_pages_follow_one_another_without_gaps(client_for):
    rec = _Recording(id=uuid.uuid4())
    total = 2 * PAGE_BLOCKS + 7
    async with client_for([rec], _blocks(total)) as client:
        first = (await client.call_tool("show_recording", {"recording_id": str(rec.id)})).structured_content
        texts = [b["sentences"][0]["text"] for b in first["blocks"]]
        cursor = first["next_cursor"]
        while cursor:
            page = (await client.call_tool(
                "app_player_blocks", {"recording_id": str(rec.id), "cursor": cursor}
            )).structured_content
            texts += [b["sentences"][0]["text"] for b in page["blocks"]]
            cursor = page["next_cursor"]
    assert texts == [f"sentence {i}" for i in range(total)]


async def test_the_app_only_tool_checks_ownership_too(client_for):
    # The server never trusts the view: a page goes through the same check as
    # the entry tool.
    rec = _Recording(id=uuid.uuid4(), user_id=uuid.uuid4())
    async with client_for([rec], _blocks(PAGE_BLOCKS + 1)) as client:
        result = await client.call_tool(
            "app_player_blocks", {"recording_id": str(rec.id), "cursor": "100"}, raise_on_error=False
        )
    assert result.is_error


@pytest.mark.parametrize("cursor", ["abc", "-1", "999"])
def test_a_cursor_it_did_not_issue_is_refused(cursor):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        page_of_blocks(_blocks(10), cursor, size=4)


def test_the_last_page_has_no_cursor():
    page, nxt = page_of_blocks(_blocks(8), "4", size=4)
    assert [b["sentences"][0]["text"] for b in page] == ["sentence 4", "sentence 5", "sentence 6", "sentence 7"]
    assert nxt is None

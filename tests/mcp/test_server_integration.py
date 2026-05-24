from __future__ import annotations

import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import pytest
from fastmcp import Client

from tests.mcp.conftest import FakeRepo, FakeTask, FakeUser
from vts.db.models import TaskStatus


@asynccontextmanager
async def _fake_session_factory_cm():
    """Mimics async_sessionmaker() — returns a context manager that yields a no-op session."""
    yield object()  # The session object is never inspected because Repo is also patched.


def _fake_session_factory():
    """Returns a callable that produces the async context manager above."""
    return _fake_session_factory_cm


async def test_server_integration_list_tasks_smoke(monkeypatch) -> None:
    """End-to-end: build_mcp_server() registers list_tasks; invoke via in-process Client.

    Smoke-test confirms the auth-and-session-and-repo wiring in each @mcp.tool wrapper
    fires correctly. Auth, sessionmaker, and Repo are all patched to keep the test
    hermetic — this is a wiring-glue test, not a DB integration test.
    """
    # Seed a FakeRepo with one task owned by alice
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    task = FakeTask(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user.id),
        source_url="https://x/abc",
        source_title="Test video",
        status=TaskStatus.completed,
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )
    repo.tasks[task.id] = task

    # Import the server module fresh so monkeypatch can swap its imported names
    import vts.mcp.server as server_mod
    from vts.core.config import Settings

    # Three patches in vts.mcp.server:
    # mcp_authenticate now calls get_http_request() internally, so only the
    # function itself needs patching here.
    async def _fake_mcp_authenticate(session):
        return user, Settings()

    monkeypatch.setattr(server_mod, "mcp_authenticate", _fake_mcp_authenticate)
    monkeypatch.setattr(server_mod, "get_db_session_factory", _fake_session_factory)
    monkeypatch.setattr(server_mod, "Repo", lambda _session: repo)

    mcp = server_mod.build_mcp_server()
    async with Client(mcp) as client:
        result = await client.call_tool("list_tasks", {})

    # The tool returns list[TaskSummary]; FastMCP deserializes the structured content
    # into a list of Root namedtuple-like objects (not dicts, not Pydantic models).
    # Access fields as attributes.
    assert result.is_error is False
    data = result.data
    assert isinstance(data, list)
    assert len(data) == 1
    summary = data[0]
    assert summary.title == "Test video"
    assert summary.url == "https://x/abc"
    assert summary.status == "completed"

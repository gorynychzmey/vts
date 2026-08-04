"""GET /api/tasks filters, and the same filters over MCP (vts-rhx, VOS-84b).

VOS-84 asks for the filter on all surfaces ("Не забыть добавить соответствующие
методы в MCP"), so the MCP path is covered here alongside REST rather than left
to the web UI.
"""
from __future__ import annotations

import uuid
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from vts.db.models import Task, TaskStatus

from tests.conftest import _TEST_USER_ID

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER_ID = uuid.UUID(_TEST_USER_ID)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value) -> None:
        self.store[key] = value.encode("utf-8") if isinstance(value, str) else value


@pytest.fixture(autouse=True)
def _wire_redis(authed_app):
    app, _factory = authed_app
    app.state.redis = _FakeRedis()


@pytest_asyncio.fixture
async def seeded(authed_app):
    """Three tasks: an upload, a link, and a link with a distinctive title."""
    _app, factory = authed_app
    made = {}
    async with factory() as session:
        rows = {
            "upload": Task(
                id=uuid.uuid4(), user_id=USER_ID, source_url="file://standup.m4a",
                source_title="Standup recording", status=TaskStatus.completed,
                artifact_dir="/tmp/a", created_at=BASE,
            ),
            "link": Task(
                id=uuid.uuid4(), user_id=USER_ID, source_url="https://youtube.com/watch?v=abc",
                source_title="Conference talk", status=TaskStatus.completed,
                artifact_dir="/tmp/b", created_at=BASE + timedelta(days=1),
            ),
            "later": Task(
                id=uuid.uuid4(), user_id=USER_ID, source_url="https://example.com/standup",
                source_title="Weekly sync", status=TaskStatus.completed,
                artifact_dir="/tmp/c", created_at=BASE + timedelta(days=5),
            ),
        }
        for row in rows.values():
            session.add(row)
        await session.commit()
        made = {k: str(v.id) for k, v in rows.items()}
    return made


async def _ids(client, query: str):
    resp = await client.get(f"/api/tasks?{query}")
    assert resp.status_code == 200, resp.text
    return {t["id"] for t in resp.json()}


@pytest.mark.asyncio
async def test_no_filter_returns_everything(seeded, client):
    assert await _ids(client, "") == set(seeded.values())


@pytest.mark.asyncio
async def test_search_matches_title_or_url(seeded, client):
    """"standup" appears in one task's TITLE and another's URL; both match."""
    assert await _ids(client, "q=standup") == {seeded["upload"], seeded["later"]}


@pytest.mark.asyncio
async def test_search_is_case_insensitive(seeded, client):
    assert await _ids(client, "q=CONFERENCE") == {seeded["link"]}


@pytest.mark.asyncio
async def test_filter_by_source_type(seeded, client):
    assert await _ids(client, "source_type=file") == {seeded["upload"]}
    assert await _ids(client, "source_type=url") == {seeded["link"], seeded["later"]}


@pytest.mark.asyncio
async def test_filter_by_created_range(seeded, client):
    frm = quote((BASE + timedelta(days=1)).isoformat())
    assert await _ids(client, f"created_from={frm}") == {seeded["link"], seeded["later"]}
    to = quote((BASE + timedelta(days=1)).isoformat())
    assert await _ids(client, f"created_to={to}") == {seeded["upload"], seeded["link"]}


@pytest.mark.asyncio
async def test_filters_combine(seeded, client):
    frm = quote((BASE + timedelta(days=2)).isoformat())
    assert await _ids(client, f"q=standup&created_from={frm}") == {seeded["later"]}


@pytest.mark.asyncio
async def test_invalid_source_type_is_rejected(seeded, client):
    resp = await client.get("/api/tasks?source_type=magnet")
    assert resp.status_code == 422
    assert "source_type" in resp.text


@pytest.mark.asyncio
async def test_inverted_date_range_is_rejected(seeded, client):
    """Rejected rather than returning nothing: an empty list would read as
    'you have no tasks' instead of 'your range is backwards'."""
    frm = quote((BASE + timedelta(days=5)).isoformat())
    to = quote(BASE.isoformat())
    resp = await client.get(f"/api/tasks?created_from={frm}&created_to={to}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_filtering_still_paginates(seeded, client):
    """A filtered request without a cursor must behave like the unfiltered
    one: newest first, honouring limit."""
    resp = await client.get("/api/tasks?source_type=url&limit=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == seeded["later"], "expected the newest matching task"


@pytest.mark.asyncio
async def test_compact_mode_still_works_with_filters(seeded, client):
    resp = await client.get("/api/tasks?source_type=file&compact=true")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


# --- MCP parity (the VOS-84 requirement) -----------------------------------


@pytest.mark.asyncio
async def test_mcp_list_tasks_supports_the_same_filters(authed_app):
    from tests.mcp.conftest import FakeRepo, FakeTask, FakeUser
    from vts.mcp.tools import list_tasks

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    uid = uuid.UUID(user.id)
    upload = FakeTask(id=uuid.uuid4(), user_id=uid, source_url="file://standup.m4a",
                      source_title="Standup", created_at=BASE)
    link = FakeTask(id=uuid.uuid4(), user_id=uid, source_url="https://youtube.com/x",
                    source_title="Talk", created_at=BASE + timedelta(days=1))
    for t in (upload, link):
        repo.tasks[t.id] = t

    by_type = await list_tasks(user=user, repo=repo, source_type="file")
    assert [t.task_id for t in by_type.tasks] == [upload.id]

    by_text = await list_tasks(user=user, repo=repo, q="talk")
    assert [t.task_id for t in by_text.tasks] == [link.id]

    by_date = await list_tasks(user=user, repo=repo, created_from=BASE + timedelta(hours=12))
    assert [t.task_id for t in by_date.tasks] == [link.id]


@pytest.mark.asyncio
async def test_mcp_rejects_an_inverted_date_range(authed_app):
    from fastapi import HTTPException

    from tests.mcp.conftest import FakeRepo, FakeUser
    from vts.mcp.tools import list_tasks

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    with pytest.raises(HTTPException) as exc:
        await list_tasks(
            user=user, repo=FakeRepo(),
            created_from=BASE + timedelta(days=5), created_to=BASE,
        )
    assert exc.value.status_code == 422

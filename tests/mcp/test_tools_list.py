from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import list_tasks
from vts.mcp.schemas import TaskPage


def _seed(repo: FakeRepo, user_id: uuid.UUID, n: int) -> list[FakeTask]:
    base = datetime(2026, 7, 30, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        t = FakeTask(
            id=uuid.uuid4(), user_id=user_id, source_url=f"https://x/{i}",
            source_title=f"title-{i}",
            status="completed" if i % 2 == 0 else "running",
            created_at=base + timedelta(minutes=i),   # task n-1 is newest
            updated_at=base + timedelta(minutes=i),
        )
        repo.tasks[t.id] = t
        out.append(t)
    return out


@pytest.mark.asyncio
async def test_first_page_newest_first_with_cursor_when_more():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 5)
    page = await list_tasks(user=user, repo=repo, limit=2)
    assert isinstance(page, TaskPage)
    # newest-first: minutes 4,3
    assert [str(t.url) for t in page.tasks] == ["https://x/4", "https://x/3"]
    assert page.has_more is True
    assert page.next_cursor is not None


@pytest.mark.asyncio
async def test_second_page_via_cursor_no_overlap():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 5)
    p1 = await list_tasks(user=user, repo=repo, limit=2)
    p2 = await list_tasks(user=user, repo=repo, limit=2, cursor=p1.next_cursor)
    assert [t.url for t in p2.tasks] == ["https://x/2", "https://x/1"]
    assert p2.has_more is True
    # no overlap between pages
    assert set(t.task_id for t in p1.tasks).isdisjoint(t.task_id for t in p2.tasks)


@pytest.mark.asyncio
async def test_last_page_has_no_cursor():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 3)
    page = await list_tasks(user=user, repo=repo, limit=10)
    assert len(page.tasks) == 3
    assert page.has_more is False
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_status_filter():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 4)   # completed at i=0,2
    page = await list_tasks(user=user, repo=repo, status="completed", limit=10)
    assert all(t.status == "completed" for t in page.tasks)
    assert len(page.tasks) == 2


@pytest.mark.asyncio
async def test_invalid_cursor_is_422():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, cursor="!!!not-a-cursor!!!")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_limit_out_of_range_is_422():
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, limit=999)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_exact_multiple_page_does_not_claim_a_further_page():
    """has_more must be false on the last page even when it is exactly full.

    vts-a95z: has_more used to be inferred from "we got exactly `limit` rows",
    but list_tasks_page returns at most `limit` and never over-fetched, so a
    final page that happened to be full was indistinguishable from a full page
    with more behind it. A client looping `while has_more` then made one wasted
    round trip and briefly saw has_more=True with nothing left.

    4 tasks, limit 2: page 2 is the last one AND exactly full.
    """
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 4)

    page1 = await list_tasks(user=user, repo=repo, limit=2)
    assert [str(t.url) for t in page1.tasks] == ["https://x/3", "https://x/2"]
    assert page1.has_more is True
    assert page1.next_cursor is not None

    page2 = await list_tasks(user=user, repo=repo, limit=2, cursor=page1.next_cursor)
    assert [str(t.url) for t in page2.tasks] == ["https://x/1", "https://x/0"]
    assert page2.has_more is False, "the last page is exactly full but there is nothing after it"
    assert page2.next_cursor is None


@pytest.mark.asyncio
async def test_walking_every_page_visits_each_task_once():
    """The over-fetch must not drop or duplicate the probe row.

    Fetching limit+1 and returning limit is easy to get subtly wrong — return
    the probe row and the next page skips it; take the cursor from the probe
    row and the next page starts one too far. Walk the whole list and check the
    set, which catches both.
    """
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 7)

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous bound; the loop should end on its own
        page = await list_tasks(user=user, repo=repo, limit=3, cursor=cursor)
        seen.extend(str(t.url) for t in page.tasks)
        if not page.has_more:
            break
        cursor = page.next_cursor
    else:
        raise AssertionError("pagination never reported has_more=False")

    assert seen == [f"https://x/{i}" for i in range(6, -1, -1)]
    assert len(set(seen)) == 7, f"a task was returned twice: {seen}"

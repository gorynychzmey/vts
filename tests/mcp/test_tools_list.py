from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import list_tasks


def _seed(repo: FakeRepo, user_id: uuid.UUID, n: int = 3) -> list[FakeTask]:
    base = datetime.now(tz=timezone.utc)
    tasks = []
    for i in range(n):
        t = FakeTask(
            id=uuid.uuid4(),
            user_id=user_id,
            source_url=f"https://x/{i}",
            source_title=f"title-{i}",
            status="completed" if i % 2 == 0 else "running",
            created_at=base - timedelta(minutes=10 - i),
            updated_at=base - timedelta(minutes=5 - i),
        )
        repo.tasks[t.id] = t
        tasks.append(t)
    return tasks


@pytest.mark.asyncio
async def test_list_tasks_default_sort_is_updated_at_desc() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 3)

    out = await list_tasks(user=user, repo=repo, status=None, limit=20, sort="updated_at", order="desc")
    times = [r.updated_at for r in out]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_list_tasks_status_filter() -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    _seed(repo, uuid.UUID(user.id), 4)

    out = await list_tasks(user=user, repo=repo, status="completed", limit=20, sort="updated_at", order="desc")
    assert all(r.status == "completed" for r in out)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_list_tasks_caps_limit_at_100() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    with pytest.raises(HTTPException) as exc:
        await list_tasks(user=user, repo=repo, status=None, limit=999, sort="updated_at", order="desc")
    assert exc.value.status_code == 422

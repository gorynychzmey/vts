from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_summary


async def test_get_summary_markdown(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    summary = tmp_path / "summary.md"
    summary.write_text("# Summary\nbody", encoding="utf-8")
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", summary_path=str(summary))
    repo.tasks[t.id] = t

    res = await get_summary(task_id=t.id, user=user, repo=repo)
    assert res.content.startswith("# Summary")
    assert res.format == "markdown"


async def test_get_summary_not_ready() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x")
    repo.tasks[t.id] = t
    with pytest.raises(HTTPException) as exc:
        await get_summary(task_id=t.id, user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_summary_file_missing(tmp_path: Path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    # summary_path is set, but the file does not exist.
    t = FakeTask(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user.id),
        source_url="x",
        summary_path=str(tmp_path / "missing.md"),
    )
    repo.tasks[t.id] = t

    with pytest.raises(HTTPException) as exc:
        await get_summary(task_id=t.id, user=user, repo=repo)
    assert exc.value.status_code == 404

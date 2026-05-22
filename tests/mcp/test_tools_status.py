from __future__ import annotations

import uuid

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_status


async def test_get_status_returns_snapshot() -> None:
    from vts.db.models import TaskStatus

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    task_id = uuid.uuid4()
    t = FakeTask(id=task_id, user_id=uuid.UUID(user.id), source_url="x", status=TaskStatus.running)
    repo.tasks[task_id] = t
    repo._asr_progress[task_id] = (3, 10)

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.task_id == task_id
    assert result.status == "running"
    assert result.asr_progress.current == 3
    assert result.asr_progress.total == 10
    assert result.summary_progress.current == 0
    assert result.summary_progress.total == 0


async def test_get_status_404_when_not_owned() -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    other = FakeTask(id=uuid.uuid4(), user_id=uuid.uuid4(), source_url="x")
    repo.tasks[other.id] = other

    with pytest.raises(HTTPException) as exc:
        await get_status(task_id=other.id, user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_status_stage_from_running_step() -> None:
    from types import SimpleNamespace
    from vts.db.models import TaskStatus

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    task_id = uuid.uuid4()
    t = FakeTask(
        id=task_id,
        user_id=uuid.UUID(user.id),
        source_url="x",
        status=TaskStatus.running,
        steps=[
            SimpleNamespace(name="download", status="completed"),
            SimpleNamespace(name="transcribe", status="running"),
            SimpleNamespace(name="summarize", status="pending"),
        ],
    )
    repo.tasks[task_id] = t

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.stage == "transcribe"


async def test_get_status_no_running_step_yields_none_stage() -> None:
    from vts.db.models import TaskStatus

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    task_id = uuid.uuid4()
    t = FakeTask(
        id=task_id,
        user_id=uuid.UUID(user.id),
        source_url="x",
        status=TaskStatus.queued,
        steps=[],
    )
    repo.tasks[task_id] = t

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.stage is None


async def test_get_status_propagates_error_message_on_failure() -> None:
    from vts.db.models import TaskStatus

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    task_id = uuid.uuid4()
    t = FakeTask(
        id=task_id,
        user_id=uuid.UUID(user.id),
        source_url="x",
        status=TaskStatus.failed,
        error_message="boom",
    )
    repo.tasks[task_id] = t

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.status == "failed"
    assert result.error == "boom"

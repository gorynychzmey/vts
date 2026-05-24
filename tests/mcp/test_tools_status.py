from __future__ import annotations

import uuid

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_status


async def test_get_status_returns_snapshot() -> None:
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
            SimpleNamespace(name="transcribe_segments", status="running"),
        ],
    )
    repo.tasks[task_id] = t
    repo._asr_progress[task_id] = (3, 10)

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.task_id == task_id
    assert result.status == "running"
    assert result.progress is not None
    assert result.progress.current == 3
    assert result.progress.total == 10


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


async def test_get_status_uses_summary_progress_during_summarize_step() -> None:
    """When a summarize_* step is running, progress reflects summary_progress_for_task."""
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
        summary_progress={"current": 7, "total": 12},
        steps=[
            SimpleNamespace(name="transcribe_segments", status="completed"),
            SimpleNamespace(name="summarize_windows", status="running"),
        ],
    )
    repo.tasks[task_id] = t

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.stage == "summarize_windows"
    assert result.progress is not None
    assert result.progress.current == 7
    assert result.progress.total == 12


async def test_get_status_progress_none_for_non_progress_stage() -> None:
    """Stages like download/extract_audio don't have a numeric counter."""
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
            SimpleNamespace(name="download", status="running"),
        ],
    )
    repo.tasks[task_id] = t

    result = await get_status(task_id=task_id, user=user, repo=repo)
    assert result.stage == "download"
    assert result.progress is None

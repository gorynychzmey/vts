from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.mcp.conftest import FakeBus, FakeRepo, FakeUser
from vts.mcp.tools import submit_video


async def test_submit_video_creates_task_notifies_and_publishes(tmp_path: Path) -> None:
    user_id = uuid.uuid4()
    user = FakeUser(id=str(user_id), username="alice")
    repo = FakeRepo()
    bus = FakeBus()

    result = await submit_video(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        user=user,
        repo=repo,
        bus=bus,
        artifacts_root=tmp_path,
    )

    assert result.status == "queued"
    assert result.task_id in repo.tasks
    assert repo.tasks[result.task_id].source_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert bus.queued_notifications == 1
    # Exactly one task_status=queued event published, for this task and user
    assert len(bus.published) == 1
    evt = bus.published[0]
    assert evt["event"] == "task_status"
    assert evt["data"] == {"status": "queued"}
    assert evt["user_id"] == str(user_id)
    assert evt["task_id"] == str(result.task_id)
    # artifact_dir was created on disk
    assert Path(repo.tasks[result.task_id].artifact_dir).is_dir()


async def test_submit_video_strips_whitespace(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    result = await submit_video(
        url="  https://x/abc  ",
        user=user,
        repo=repo,
        bus=bus,
        artifacts_root=tmp_path,
    )
    assert repo.tasks[result.task_id].source_url == "https://x/abc"


async def test_submit_video_rejects_blank_url(tmp_path: Path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="   ",
            user=user,
            repo=repo,
            bus=bus,
            artifacts_root=tmp_path,
        )
    assert exc.value.status_code == 422


async def test_submit_video_defaults_match_web_pipeline(tmp_path: Path) -> None:
    """vts-08l: with no params besides url, MCP must produce the same
    task.options as web does on a bare /api/tasks POST — full pipeline."""
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    result = await submit_video(
        url="https://x/abc",
        user=user,
        repo=repo,
        bus=bus,
        artifacts_root=tmp_path,
    )
    opts = repo.tasks[result.task_id].options
    assert opts == {
        "language": None,
        "audio_only": False,
        "transcript": True,
        "summary": True,
    }


async def test_submit_video_passes_through_explicit_options(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    result = await submit_video(
        url="https://x/abc",
        user=user,
        repo=repo,
        bus=bus,
        artifacts_root=tmp_path,
        language="en",
        audio_only=True,
        transcript=True,
        summary=False,
    )
    opts = repo.tasks[result.task_id].options
    assert opts == {
        "language": "en",
        "audio_only": True,
        "transcript": True,
        "summary": False,
    }


async def test_submit_video_rejects_summary_without_transcript(tmp_path: Path) -> None:
    """Mirrors TaskCreateRequest.validate_stage_dependencies in web."""
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    bus = FakeBus()
    with pytest.raises(HTTPException) as exc:
        await submit_video(
            url="https://x/abc",
            user=user,
            repo=repo,
            bus=bus,
            artifacts_root=tmp_path,
            transcript=False,
            summary=True,
        )
    assert exc.value.status_code == 422
    assert "transcript" in exc.value.detail.lower()

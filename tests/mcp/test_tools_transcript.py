from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.mcp.conftest import FakeRepo, FakeUser, FakeTask
from vts.mcp.tools import get_transcript


async def test_get_transcript_raw_txt(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("hello world", encoding="utf-8")
    t = FakeTask(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user.id),
        source_url="x",
        transcript_path=str(transcript),
        artifact_dir=str(tmp_path),
    )
    repo.tasks[t.id] = t

    res = await get_transcript(task_id=t.id, variant="raw", user=user, repo=repo)
    assert res.content == "hello world"
    assert res.format == "txt"


async def test_get_transcript_redacted(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "redacted_transcript.txt").write_text("redacted ok", encoding="utf-8")
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", artifact_dir=str(tmp_path))
    repo.tasks[t.id] = t

    res = await get_transcript(task_id=t.id, variant="redacted", user=user, repo=repo)
    assert res.content == "redacted ok"
    assert res.format == "txt"


async def test_get_transcript_raw_not_ready(tmp_path: Path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()))
    repo = FakeRepo()
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", artifact_dir=str(tmp_path))
    repo.tasks[t.id] = t

    with pytest.raises(HTTPException) as exc:
        await get_transcript(task_id=t.id, variant="raw", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_transcript_redacted_not_ready(tmp_path: Path) -> None:
    from fastapi import HTTPException

    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    # artifact_dir exists but outputs/redacted_transcript.txt does not.
    artifact = tmp_path / "art"
    (artifact / "outputs").mkdir(parents=True)
    t = FakeTask(id=uuid.uuid4(), user_id=uuid.UUID(user.id), source_url="x", artifact_dir=str(artifact))
    repo.tasks[t.id] = t

    with pytest.raises(HTTPException) as exc:
        await get_transcript(task_id=t.id, variant="redacted", user=user, repo=repo)
    assert exc.value.status_code == 404


async def test_get_transcript_raw_json_format(tmp_path: Path) -> None:
    user = FakeUser(id=str(uuid.uuid4()), username="alice")
    repo = FakeRepo()
    transcript = tmp_path / "transcript.json"
    transcript.write_text('{"text": "hello"}', encoding="utf-8")
    t = FakeTask(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user.id),
        source_url="x",
        transcript_path=str(transcript),
        artifact_dir=str(tmp_path),
    )
    repo.tasks[t.id] = t

    res = await get_transcript(task_id=t.id, variant="raw", user=user, repo=repo)
    assert res.format == "json"
    assert res.content == '{"text": "hello"}'

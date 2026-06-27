from __future__ import annotations

import uuid
from datetime import datetime, timezone

from vts.mcp.schemas import (
    ProgressCounts,
    PromptInfo,
    PromptResult,
    SubmitVideoResult,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)


def test_submit_video_result_shape() -> None:
    r = SubmitVideoResult(task_id=uuid.uuid4(), status="queued", created_at=datetime.now(tz=timezone.utc))
    d = r.model_dump(mode="json")
    assert set(d) == {"task_id", "status", "created_at"}


def test_task_summary_shape() -> None:
    r = TaskSummary(
        task_id=uuid.uuid4(),
        status="completed",
        title="hi",
        url="https://x",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )
    d = r.model_dump(mode="json")
    assert set(d) == {"task_id", "status", "title", "url", "created_at", "updated_at"}


def test_task_status_result_includes_progress() -> None:
    r = TaskStatusResult(
        task_id=uuid.uuid4(),
        status="running",
        stage="transcribe_segments",
        progress=ProgressCounts(current=5, total=10),
        error=None,
        updated_at=datetime.now(tz=timezone.utc),
    )
    d = r.model_dump(mode="json")
    assert d["progress"] == {"current": 5, "total": 10}
    assert "asr_progress" not in d
    assert "summary_progress" not in d


def test_transcript_shape() -> None:
    tr = TranscriptResult(task_id=uuid.uuid4(), variant="raw", content="abc", format="txt")
    assert tr.format in {"txt", "json"}


def test_prompt_info_and_result_shapes() -> None:
    info = PromptInfo(source="system", id="summary", name="Summary", editable=False)
    d = info.model_dump(mode="json")
    assert set(d) == {"source", "id", "name", "editable"}
    assert d["editable"] is False

    res = PromptResult(task_id=uuid.uuid4(), source="system", id="summary", content="# md")
    rd = res.model_dump(mode="json")
    assert set(rd) == {"task_id", "source", "id", "content"}
    assert rd["content"] == "# md"


def test_wait_result_reached_flag() -> None:
    r = WaitResult(
        task_id=uuid.uuid4(),
        status="completed",
        reached=True,
        stage="done",
        updated_at=datetime.now(tz=timezone.utc),
    )
    assert r.reached is True

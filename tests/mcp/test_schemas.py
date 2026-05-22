from __future__ import annotations

import uuid
from datetime import datetime, timezone

from vts.mcp.schemas import (
    ProgressCounts,
    SubmitVideoResult,
    SummaryResult,
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
        stage="transcribing",
        asr_progress=ProgressCounts(current=5, total=10),
        summary_progress=ProgressCounts(current=0, total=0),
        error=None,
        updated_at=datetime.now(tz=timezone.utc),
    )
    d = r.model_dump(mode="json")
    assert d["asr_progress"] == {"current": 5, "total": 10}
    assert d["summary_progress"] == {"current": 0, "total": 0}


def test_transcript_and_summary_shapes() -> None:
    tr = TranscriptResult(task_id=uuid.uuid4(), variant="raw", content="abc", format="txt")
    su = SummaryResult(task_id=uuid.uuid4(), content="# md", format="markdown")
    assert tr.format in {"txt", "json"}
    assert su.format == "markdown"


def test_wait_result_reached_flag() -> None:
    r = WaitResult(
        task_id=uuid.uuid4(),
        status="completed",
        reached=True,
        stage="done",
        updated_at=datetime.now(tz=timezone.utc),
    )
    assert r.reached is True

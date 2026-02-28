from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

from vts.api.main import _summary_progress_for_task, serialize_task
from vts.db.models import StepStatus, TaskStatus


def _step(name: str, status: StepStatus) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        status=status,
        attempt=1,
        started_at=None,
        finished_at=None,
        message=None,
    )


def _task(artifact_dir: Path, *, steps: list[SimpleNamespace], options: dict[str, object] | None = None) -> SimpleNamespace:
    now = datetime.now(tz=timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_url="https://example.com/video",
        status=TaskStatus.running,
        options=options if options is not None else {"transcript": True, "summary": True},
        transcript_path=None,
        summary_path=None,
        error_message=None,
        created_at=now,
        updated_at=now,
        artifact_dir=str(artifact_dir),
        steps=steps,
    )


def test_summary_progress_uses_windows_and_final_running(tmp_path: Path) -> None:
    summary_dir = tmp_path / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / "chunks.json").write_text(
        json.dumps({"chunks": ["c1", "c2", "c3"]}),
        encoding="utf-8",
    )
    (summary_dir / "windows.json").write_text(
        json.dumps({"windows": [{"window_index": 1}, {"window_index": 2}]}),
        encoding="utf-8",
    )
    task = _task(
        tmp_path,
        steps=[_step("summarize_windows", StepStatus.completed), _step("summarize_final", StepStatus.running)],
    )

    current, total = _summary_progress_for_task(task)

    assert (current, total) == (3, 4)


def test_serialize_task_includes_transcribe_and_summary_progress(tmp_path: Path) -> None:
    summary_dir = tmp_path / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    task = _task(
        tmp_path,
        steps=[_step("download", StepStatus.completed), _step("summarize_final", StepStatus.completed)],
    )

    payload = serialize_task(
        task,
        queue_positions={task.id: 2},
        asr_progress={task.id: (7, 13)},
        summary_progress={task.id: (1, 1)},
    )

    assert payload.queue_position == 2
    assert payload.progress.transcribe.current == 7
    assert payload.progress.transcribe.total == 13
    assert payload.progress.summary.current == 1
    assert payload.progress.summary.total == 1
    assert payload.failure_code is None


def test_serialize_task_sets_failure_code_for_live_not_started(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[_step("download", StepStatus.failed)],
    )
    task.status = TaskStatus.failed
    task.error_message = "ERROR: [youtube] wdEo7uHeWgs: This live event will begin in a few moments."

    payload = serialize_task(task)

    assert payload.failure_code == "download_live_not_started"

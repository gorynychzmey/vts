from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

from vts.api.main import ARCHIVED_LOG_MESSAGE, _archive_task_artifacts, serialize_task
from vts.services.task_progress import summary_progress_for_task as _summary_progress_for_task
from vts.db.models import StepStatus, TaskStatus


def _step(
    name: str,
    status: StepStatus,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        status=status,
        attempt=1,
        started_at=started_at,
        finished_at=finished_at,
        message=None,
    )


def _task(
    artifact_dir: Path,
    *,
    steps: list[SimpleNamespace],
    options: dict[str, object] | None = None,
    summary_progress: dict[str, int] | None = None,
) -> SimpleNamespace:
    now = datetime.now(tz=timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_url="https://example.com/video",
        source_title=None,
        status=TaskStatus.running,
        options=options if options is not None else {"transcript": True, "summary": True},
        transcript_path=None,
        summary_path=None,
        error_message=None,
        summary_progress=summary_progress,
        created_at=now,
        updated_at=now,
        artifact_dir=str(artifact_dir),
        steps=steps,
    )


def test_summary_progress_reads_from_db_field(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[],
        summary_progress={"current": 3, "total": 4},
    )

    current, total = _summary_progress_for_task(task)

    assert (current, total) == (3, 4)


def test_summary_progress_returns_zero_when_field_missing(tmp_path: Path) -> None:
    task = _task(tmp_path, steps=[])

    current, total = _summary_progress_for_task(task)

    assert (current, total) == (0, 0)


def test_summary_progress_returns_zero_when_summary_disabled(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[],
        options={"transcript": True, "summary": False},
        summary_progress={"current": 3, "total": 4},
    )

    current, total = _summary_progress_for_task(task)

    assert (current, total) == (0, 0)


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


def test_serialize_task_includes_completed_stats(tmp_path: Path) -> None:
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = outputs_dir / "transcript.txt"
    summary_path = outputs_dir / "summary.md"
    redacted_path = outputs_dir / "redacted_transcript.txt"
    transcript_text = "Hello world"
    summary_text = "Summary body"
    redacted_text = "Segment one\nSegment two\n"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    summary_path.write_text(summary_text, encoding="utf-8")
    redacted_path.write_text(redacted_text, encoding="utf-8")

    started = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(minutes=2, seconds=5)
    task = _task(
        tmp_path,
        steps=[
            _step("download", StepStatus.completed, started_at=started, finished_at=started + timedelta(seconds=20)),
            _step("merge_transcript", StepStatus.completed, started_at=started + timedelta(seconds=20), finished_at=finished),
        ],
    )
    task.status = TaskStatus.completed
    task.transcript_path = str(transcript_path)
    task.summary_path = str(summary_path)

    payload = serialize_task(task)

    assert payload.stats.processing_seconds == 125
    assert payload.stats.transcript_chars == len(transcript_text)
    assert payload.stats.summary_chars == len(summary_text)
    assert payload.stats.redacted_chars == len(redacted_text.strip())
    # No media file on disk → media stats are absent rather than zero.
    assert payload.stats.media_seconds is None
    assert payload.stats.media_bytes is None


def test_serialize_task_includes_media_size_and_cached_duration(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_file = media_dir / "audio.original.m4a"
    media_bytes = b"x" * 2048
    media_file.write_bytes(media_bytes)
    # Pre-seed the probe sidecar so duration is read from cache, keyed on the
    # file's current size+mtime — avoids needing ffprobe in the test env.
    stat = media_file.stat()
    sidecar = media_file.with_suffix(media_file.suffix + ".probe.json")
    sidecar.write_text(
        json.dumps({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "seconds": 754}),
        encoding="utf-8",
    )

    task = _task(tmp_path, steps=[])
    payload = serialize_task(task)

    assert payload.stats.media_bytes == len(media_bytes)
    assert payload.stats.media_seconds == 754


def test_archive_task_artifacts_keeps_transcript_and_summary(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    media_dir = tmp_path / "media"
    outputs_dir = tmp_path / "outputs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = outputs_dir / "transcript.txt"
    summary_path = outputs_dir / "summary.md"
    transcript_path.write_text("hello", encoding="utf-8")
    summary_path.write_text("world", encoding="utf-8")
    (logs_dir / "task.log").write_text("old log", encoding="utf-8")
    (media_dir / "video.mkv").write_text("video", encoding="utf-8")
    (outputs_dir / "segments_manifest.json").write_text("{}", encoding="utf-8")

    task = SimpleNamespace(
        artifact_dir=str(tmp_path),
        transcript_path=str(transcript_path),
        summary_path=str(summary_path),
    )

    _archive_task_artifacts(task)

    assert transcript_path.exists()
    assert summary_path.exists()
    assert not (media_dir / "video.mkv").exists()
    assert not (outputs_dir / "segments_manifest.json").exists()
    assert (logs_dir / "task.log").read_text(encoding="utf-8").strip() == ARCHIVED_LOG_MESSAGE


def test_serialize_task_preserves_archived_status(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[_step("download", StepStatus.completed)],
    )
    task.status = TaskStatus.archived

    payload = serialize_task(task)

    assert payload.status == TaskStatus.archived.value

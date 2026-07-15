from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

from vts.api.main import ARCHIVED_LOG_MESSAGE, _archive_task_artifacts, _get_lane_positions, serialize_task
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


def test_serialize_waiting_task_carries_lane_queue(tmp_path: Path) -> None:
    task = _task(tmp_path, steps=[])
    task.status = TaskStatus.waiting

    payload = serialize_task(task, lane_positions={task.id: ("gpu", 2)})

    assert payload.queue == "gpu"
    assert payload.queue_position == 2


def test_serialize_queued_task_keeps_global_position(tmp_path: Path) -> None:
    task = _task(tmp_path, steps=[])
    task.status = TaskStatus.queued

    payload = serialize_task(task, queue_positions={task.id: 3})

    assert payload.queue is None
    assert payload.queue_position == 3


class _FakeLaneRedis:
    """Minimal async Redis stub exposing only `get`, backed by a fixed JSON
    payload — enough to exercise `_get_lane_positions` parsing."""

    def __init__(self, payload: dict[str, list[str]] | None = None, *, raw: bytes | str | None = None) -> None:
        self._payload = raw if raw is not None else json.dumps(payload or {})

    async def get(self, key: str) -> str | bytes:
        return self._payload


async def test_get_lane_positions_parses_and_dedupes() -> None:
    task_a = uuid.uuid4()
    task_b = uuid.uuid4()
    task_c = uuid.uuid4()
    redis = _FakeLaneRedis(
        {
            "network": [str(task_a)],
            "ffmpeg": [],
            "gpu_asr": [str(task_b)],
            "gpu_llm": [str(task_c)],
        }
    )

    positions = await _get_lane_positions(redis, "vts:")

    # gpu_asr and gpu_llm share a single "gpu" counter, with asr numbered
    # first (scheduling priority in LaneManager) — so task_b (asr) gets
    # position 1 and task_c (llm) gets position 2, not two separate 1s.
    assert positions == {
        task_a: ("network", 1),
        task_b: ("gpu", 1),
        task_c: ("gpu", 2),
    }


async def test_get_lane_positions_dedupes_across_asr_and_llm() -> None:
    task_b = uuid.uuid4()
    task_c = uuid.uuid4()
    redis = _FakeLaneRedis(
        {
            "gpu_asr": [str(task_b), str(task_b)],
            "gpu_llm": [str(task_c)],
        }
    )

    positions = await _get_lane_positions(redis, "vts:")

    assert positions == {
        task_b: ("gpu", 1),
        task_c: ("gpu", 2),
    }


async def test_get_lane_positions_malformed_json_returns_empty() -> None:
    redis = _FakeLaneRedis(raw=b"not json")

    positions = await _get_lane_positions(redis, "vts:")

    assert positions == {}


async def test_get_lane_positions_skips_non_uuid_entries() -> None:
    task_a = uuid.uuid4()
    redis = _FakeLaneRedis(
        {
            "gpu_asr": ["not-a-uuid"],
            "gpu_llm": [str(task_a)],
        }
    )

    positions = await _get_lane_positions(redis, "vts:")

    assert positions == {task_a: ("gpu", 1)}


# --- capabilities (vts-c2n) ---

_SUMMARY_OPTIONS: dict[str, object] = {"prompts": [{"source": "system", "id": "summary"}]}


def test_capabilities_completed_summary_task_can_restart_summary(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[_step("summarize_windows", StepStatus.completed)],
        options=_SUMMARY_OPTIONS,
    )
    task.status = TaskStatus.completed

    payload = serialize_task(task)

    assert payload.capabilities.can_restart_summary is True
    assert payload.capabilities.can_restart_final_summary is True


def test_capabilities_queued_task_has_no_restart_capabilities(tmp_path: Path) -> None:
    task = _task(tmp_path, steps=[], options=_SUMMARY_OPTIONS)
    task.status = TaskStatus.queued

    payload = serialize_task(task)

    assert payload.capabilities.can_restart_summary is False
    assert payload.capabilities.can_restart_final_summary is False


def test_capabilities_failed_final_summary_can_restart_final_only(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[
            _step("summarize_windows", StepStatus.completed),
            _step("summarize_final", StepStatus.failed),
        ],
        options=_SUMMARY_OPTIONS,
    )
    task.status = TaskStatus.failed

    payload = serialize_task(task)

    # summary restart needs a failed SUMMARY_STEP_NAMES step: summarize_final qualifies
    assert payload.capabilities.can_restart_summary is True
    assert payload.capabilities.can_restart_final_summary is True


def test_capabilities_completed_without_summary_prompt_cannot_restart_summary(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        steps=[_step("summarize_windows", StepStatus.completed)],
        options={"prompts": [{"source": "user", "id": "custom"}]},
    )
    task.status = TaskStatus.completed

    payload = serialize_task(task)

    assert payload.capabilities.can_restart_summary is False
    # final-summary restart does not depend on prompt selection
    assert payload.capabilities.can_restart_final_summary is True


def test_capabilities_compact_serializer_matches_full(tmp_path: Path) -> None:
    from vts.api.main import serialize_task_compact

    task = _task(
        tmp_path,
        steps=[_step("summarize_windows", StepStatus.completed)],
        options=_SUMMARY_OPTIONS,
    )
    task.status = TaskStatus.completed

    compact = serialize_task_compact(task)

    assert compact.capabilities == serialize_task(task).capabilities

"""Turning a Task row into the API's task shape, and the predicates it needs.

`serialize_task` is the single most shared helper in the API — four routers
render tasks — so it lives here rather than in any one of them.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from vts.api._helpers.base import SUMMARY_STEP_NAMES, _find_media_file
from vts.api.schemas import TaskCompactOut, TaskOut
from vts.core.failures import classify_failure_code
from vts.db.models import StepStatus, Task, TaskStatus
from vts.services import task_status as _ts
from vts.services.media import probe_duration
from vts.services.task_progress import selected_prompt_refs

logger = logging.getLogger(__name__)

def can_pause_task(status: TaskStatus) -> bool:
    return _ts.can_pause(status)

def can_resume_task(status: TaskStatus) -> bool:
    return _ts.can_resume(status)

def can_restart_summary_task(task: Task) -> bool:
    refs = selected_prompt_refs(task.options if isinstance(task.options, dict) else {})
    summary_selected = any(r["source"] == "system" and r["id"] == "summary" for r in refs)
    if not summary_selected:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return any(step.name in SUMMARY_STEP_NAMES and step.status == StepStatus.failed for step in task.steps)

def can_restart_final_summary_task(task: Task) -> bool:
    # Mirrors the frontend's `summaryExpected`: ANY selected prompt yields a
    # finalize step (system:summary -> summarize_final, anything else ->
    # finalize:<source>:<id>), so restarting the final summary only requires that
    # at least one prompt is selected. This is deliberately weaker than
    # can_restart_summary_task's system:summary gate above -- restarting *the
    # summary* is meaningless without the summary prompt, but restarting the
    # finalize tail is valid for a user-prompt-only task.
    refs = selected_prompt_refs(task.options if isinstance(task.options, dict) else {})
    if not refs:
        return False
    summarize_windows_status = _find_step_status(task, "summarize_windows")
    if summarize_windows_status != StepStatus.completed:
        return False
    if task.status == TaskStatus.completed:
        return True
    if task.status != TaskStatus.failed:
        return False
    return _find_step_status(task, "summarize_final") == StepStatus.failed

def can_resolve_speakers_task(task: Task) -> bool:
    """The voice-resolution dialog is available once match_speakers has produced
    speaker_matches.json, for the rest of the task's life except archived/canceled.
    A task-DEPENDENT capability keyed on data availability, which a status set
    can't express.

    Keys off the PRESENCE of speaker_matches.json, NOT the match_speakers step
    status: that step completes for a NON-diarized task too (it early-returns
    without writing the file), so a step-status check would wrongly offer the
    dialog for a task that has no speakers to resolve (the button did nothing).
    The diarized path is the only one that writes the artifact, so its existence
    is the exact precondition — the same file MatchSpeakersStep.already_done and
    the resolve endpoint both read.
    """
    if task.status in {TaskStatus.archived, TaskStatus.canceled}:
        return False
    return (Path(task.artifact_dir) / "outputs" / "speaker_matches.json").exists()

def _find_step_status(task: Task, step_name: str) -> StepStatus | None:
    for step in task.steps:
        if step.name == step_name:
            return step.status
    return None

def _speaker_ordering_entries(outputs: Path, matches: dict) -> list[dict]:
    """Entries in first-appearance order for label_map, so the dialog's
    display_label matches the transcript's "Голос N" numbering (bug #2).

    Prefer diarization.json segments — they cover EVERY speaker in first-spoke
    order, including ones the transcript dropped as noise, so a noise speaker
    still gets a stable number. Fall back to transcript.json entries, then to
    the matches keys (so a label always gets some ordering). label_map only
    reads each item's "speaker", so a bare {"speaker": label} list suffices.
    """
    diar_path = outputs / "diarization.json"
    if diar_path.exists():
        try:
            diar = json.loads(diar_path.read_text(encoding="utf-8"))
            segments = diar.get("segments") if isinstance(diar, dict) else None
            if isinstance(segments, list) and segments:
                return [{"speaker": s.get("speaker")} for s in segments
                        if isinstance(s, dict) and s.get("speaker") is not None]
        except (OSError, json.JSONDecodeError):
            pass
    transcript_path = outputs / "transcript.json"
    if transcript_path.exists():
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, list) and entries:
                return [{"speaker": e.get("speaker")} for e in entries
                        if isinstance(e, dict) and e.get("speaker") is not None]
        except (OSError, json.JSONDecodeError):
            pass
    return [{"speaker": label} for label in matches]

def _processing_seconds_for_task(task: Task) -> int | None:
    # Sum each step's own duration rather than the span from the first step's
    # start to the last step's finish. A task can sit paused / awaiting input
    # for hours between steps; that idle gap is not work time, so counting the
    # raw span (max(finished) - min(started)) massively overstates it. Each
    # step carries its own started_at/finished_at (last attempt), so the sum of
    # per-step durations is the actual processing time. Pauses that happen
    # *inside* a running step are not subtracted (the step re-runs with a fresh
    # started_at on resume anyway) — only between-step idle time is excluded.
    total = 0.0
    counted = False
    for step in task.steps:
        if step.started_at is None or step.finished_at is None:
            continue
        step_seconds = (step.finished_at - step.started_at).total_seconds()
        if step_seconds < 0:
            continue
        total += step_seconds
        counted = True
    if not counted:
        return None
    return int(total)

def _text_length_from_path(path_value: str | Path | None, *, prefer_json_text_field: bool = False) -> int | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    text_value = raw_text
    if prefer_json_text_field and path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        extracted = payload.get("text")
        if not isinstance(extracted, str):
            return None
        text_value = extracted

    return len(text_value.strip())

def _task_stats_for_serialization(task: Task) -> dict[str, int | None]:
    redacted_path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
    media_file = _find_media_file(task.artifact_dir)
    media_bytes: int | None = None
    media_seconds: int | None = None
    if media_file is not None:
        try:
            media_bytes = media_file.stat().st_size
        except OSError:
            media_bytes = None
        media_seconds = _media_seconds_for_file(media_file)
    return {
        "processing_seconds": _processing_seconds_for_task(task),
        "transcript_chars": _text_length_from_path(task.transcript_path, prefer_json_text_field=True),
        "summary_chars": _text_length_from_path(task.summary_path, prefer_json_text_field=False),
        "redacted_chars": _text_length_from_path(redacted_path, prefer_json_text_field=False),
        "media_seconds": media_seconds,
        "media_bytes": media_bytes,
    }

def _media_seconds_for_file(media_file: Path) -> int | None:
    """Media (audio/video) length in whole seconds, probed via ffprobe.

    ffprobe spawns a subprocess, so the result is cached in a sidecar JSON
    keyed on the media file's size+mtime. List serialization probes each
    task at most once per file; later renders read the sidecar."""
    try:
        stat = media_file.stat()
    except OSError:
        return None
    sidecar = media_file.with_suffix(media_file.suffix + ".probe.json")
    cache_key = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    try:
        cached = json.loads(sidecar.read_text(encoding="utf-8"))
        if (
            isinstance(cached, dict)
            and cached.get("size") == cache_key["size"]
            and cached.get("mtime_ns") == cache_key["mtime_ns"]
            and isinstance(cached.get("seconds"), int)
        ):
            return cached["seconds"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        seconds = int(probe_duration(media_file))
    except (RuntimeError, ValueError):
        return None
    if seconds < 0:
        seconds = 0
    try:
        sidecar.write_text(json.dumps({**cache_key, "seconds": seconds}), encoding="utf-8")
    except OSError:
        pass  # best-effort cache; still return the freshly probed value
    return seconds

def serialize_task(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    lane_positions: dict[uuid.UUID, tuple[str, int]] | None = None,
) -> TaskOut:
    queue: str | None = None
    queue_position: int | None = None
    if task.status == TaskStatus.waiting and lane_positions is not None and task.id in lane_positions:
        queue, queue_position = lane_positions[task.id]
    elif queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    failure_code = classify_failure_code(task.error_message)
    return TaskOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        awaiting_step=task.awaiting_step,
        queue_position=queue_position,
        queue=queue,
        capabilities={
            "can_restart_summary": can_restart_summary_task(task),
            "can_restart_final_summary": can_restart_final_summary_task(task),
            "can_resolve_speakers": can_resolve_speakers_task(task),
        },
        options=task.options,
        transcript_path=task.transcript_path,
        summary_path=task.summary_path,
        redacted_path=str(Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt")
        if task.artifact_dir
        and (Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt").exists()
        else None,
        media_path=str(_mf) if (_mf := _find_media_file(task.artifact_dir)) else None,
        error_message=task.error_message,
        failure_code=failure_code,
        created_at=task.created_at,
        updated_at=task.updated_at,
        steps=[
            {
                "name": step.name,
                "status": step.status.value,
                "attempt": step.attempt,
                "started_at": step.started_at,
                "finished_at": step.finished_at,
                "message": step.message,
            }
            for step in sorted(task.steps, key=lambda item: item.name)
        ],
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
        stats=_task_stats_for_serialization(task),
    )

def serialize_task_compact(
    task: Task,
    queue_positions: dict[uuid.UUID, int] | None = None,
    asr_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    summary_progress: dict[uuid.UUID, tuple[int, int]] | None = None,
    lane_positions: dict[uuid.UUID, tuple[str, int]] | None = None,
) -> "TaskCompactOut":
    """Compact serializer for list views. Drops steps/options/paths/error
    message — see TaskCompactOut docstring for the rationale."""
    from vts.api.schemas import TaskCompactOut
    queue: str | None = None
    queue_position: int | None = None
    if task.status == TaskStatus.waiting and lane_positions is not None and task.id in lane_positions:
        queue, queue_position = lane_positions[task.id]
    elif queue_positions is not None:
        queue_position = queue_positions.get(task.id)
    transcribe_current, transcribe_total = (0, 0)
    if asr_progress is not None:
        transcribe_current, transcribe_total = asr_progress.get(task.id, (0, 0))
    summary_current, summary_total = (0, 0)
    if summary_progress is not None:
        summary_current, summary_total = summary_progress.get(task.id, (0, 0))
    return TaskCompactOut(
        id=task.id,
        source_url=task.source_url,
        source_title=task.source_title,
        status=task.status.value,
        awaiting_step=task.awaiting_step,
        queue_position=queue_position,
        queue=queue,
        capabilities={
            "can_restart_summary": can_restart_summary_task(task),
            "can_restart_final_summary": can_restart_final_summary_task(task),
            "can_resolve_speakers": can_resolve_speakers_task(task),
        },
        failure_code=classify_failure_code(task.error_message),
        created_at=task.created_at,
        updated_at=task.updated_at,
        progress={
            "transcribe": {"current": transcribe_current, "total": transcribe_total},
            "summary": {"current": summary_current, "total": summary_total},
        },
        stats=_task_stats_for_serialization(task),
    )

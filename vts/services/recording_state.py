"""Recording completeness state and its mapping from task status (vts-lzad).

A recording owns its result and says on its own whether that result is whole.
The task status cannot say it: `archived` follows both a finished and a failed
run. These helpers are pure so the status-change wiring and the migration
backfills (`vts.services.recording_backfill`) share one rule.
"""
from __future__ import annotations

from vts.db.models import RecordingState, TaskStatus
from vts.services.task_progress import selected_prompt_refs

_STATUS_TO_STATE: dict[TaskStatus, RecordingState | None] = {
    TaskStatus.completed: RecordingState.complete,
    TaskStatus.failed: RecordingState.incomplete,
    TaskStatus.canceled: RecordingState.incomplete,
    TaskStatus.queued: RecordingState.processing,
    TaskStatus.running: RecordingState.processing,
    # No transition of their own: the recording keeps whatever it had.
    TaskStatus.paused: None,
    TaskStatus.waiting: None,
    TaskStatus.awaiting_input: None,
    TaskStatus.archived: None,
}


def state_for_status(status: TaskStatus) -> RecordingState | None:
    """State a task status moves its recording to; None means leave it unchanged."""
    return _STATUS_TO_STATE[TaskStatus(status)]


def _archived_is_complete(
    *,
    transcript_path: str | None,
    summary_path: str | None,
    prompt_results: list | None,
    options: dict,
) -> bool:
    """Archiving is silent about success, so judge by what the task requested.

    Every requested prompt needs a result. The built-in system summary is
    recorded in `summary_path`; every other prompt only in the prompt-result
    index. A task that requested nothing is judged by its transcript.
    """
    refs = selected_prompt_refs(options)
    if not refs:
        return bool(transcript_path)
    done = {
        (str(e.get("source")), str(e.get("id")))
        for e in (prompt_results if isinstance(prompt_results, list) else [])
        if isinstance(e, dict) and e.get("status") == "completed"
    }
    for ref in refs:
        key = (ref["source"], ref["id"])
        if key == ("system", "summary"):
            if not summary_path:
                return False
        elif key not in done:
            return False
    return True


def initial_state(
    status: TaskStatus,
    *,
    transcript_path: str | None,
    summary_path: str | None,
    prompt_results: list | None,
    options: dict,
) -> RecordingState:
    """State for a recording that has none yet.

    Judged from the task's status, what it requested (`options`), and the
    recording's own content: its paths and prompt-result index.
    """
    mapped = state_for_status(status)
    if mapped is not None:
        return mapped
    if TaskStatus(status) is TaskStatus.archived:
        if _archived_is_complete(
            transcript_path=transcript_path,
            summary_path=summary_path,
            prompt_results=prompt_results,
            options=options,
        ):
            return RecordingState.complete
        return RecordingState.incomplete
    return RecordingState.processing  # paused / waiting / awaiting_input

"""Recording completeness state and its mapping from task status (vts-lzad).

A recording owns its result and says on its own whether that result is whole.
The task status cannot say it: `archived` follows both a finished and a failed
run. These helpers are pure so the status-change wiring and the backfill
migration share one rule.
"""
from __future__ import annotations

from sqlalchemy import text

from vts.db.models import RecordingState, TaskStatus

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


def initial_state(
    status: TaskStatus,
    *,
    transcript_path: str | None,
    summary_path: str | None,
    prompts_requested: bool,
) -> RecordingState:
    """State for a recording that has none yet, judged from its task and files."""
    mapped = state_for_status(status)
    if mapped is not None:
        return mapped
    if TaskStatus(status) is TaskStatus.archived:
        # Archiving is silent about success, so judge by what was produced.
        if summary_path or (not prompts_requested and transcript_path):
            return RecordingState.complete
        return RecordingState.incomplete
    return RecordingState.processing  # paused / waiting / awaiting_input


def backfill_recording_states(conn) -> int:
    """Set `state` on every recording; sync Connection, used by the migration."""
    rows = conn.execute(text(
        "SELECT r.id, r.transcript_path, r.summary_path, r.source_task_id, "
        "t.status AS task_status, t.options AS task_options "
        "FROM recordings r LEFT JOIN tasks t ON t.id = r.source_task_id"
    )).mappings().all()
    for row in rows:
        if row["source_task_id"] is None or row["task_status"] is None:
            state = (
                RecordingState.complete if row["transcript_path"] else RecordingState.incomplete
            )
        else:
            options = row["task_options"]
            prompts = options.get("prompts") if isinstance(options, dict) else None
            state = initial_state(
                TaskStatus(row["task_status"]),
                transcript_path=row["transcript_path"],
                summary_path=row["summary_path"],
                prompts_requested=isinstance(prompts, list) and len(prompts) > 0,
            )
        conn.execute(
            text("UPDATE recordings SET state = :state WHERE id = :id"),
            {"state": state.value, "id": row["id"]},
        )
    return len(rows)

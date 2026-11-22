"""Backfilling recording content from existing tasks (vts-lzad).

Before the split a recording was created only when the pipeline indexed a
task, and the content it owns now (`redacted_path`, `prompt_results`) lived
on the task side: the redacted transcript had no column at all, the prompt
results only in `Task.options`. This brings existing rows to the shape the
live code writes from now on:

- every task without a recording gets one, with the fields
  `Repo.upsert_recording_for_task` would give it;
- every recording that still has its task gets the redacted transcript and
  the prompt results whose files exist.

Runs from a migration, so it uses the migration's synchronous connection and
plain SQL rather than the async ORM repository. Safe to repeat: a second run
finds every task with its recording and every field already set.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from vts.db.models import RecordingState, TaskStatus, utcnow
from vts.services.recording_meta import language_code, recording_display_name
from vts.services.recording_state import initial_state

REDACTED_RELATIVE = Path("outputs") / "redacted_transcript.txt"


def _reachable(path: Path, kind: str) -> bool:
    """is_file / is_dir that treats an unreadable path as absent.

    The migration runs in the init container, where an exception (say a
    PermissionError on a mount) would block startup (vts-lzad).
    """
    try:
        return path.is_dir() if kind == "dir" else path.is_file()
    except OSError:
        return False


def artifacts_available(conn: Any, artifacts_root: Path) -> bool:
    """Whether the artifacts volume looks mounted where this runs; used for a warning.

    The image does not create the artifacts root, so a missing directory means
    the volume is not there. An empty mount point is caught by the second
    check: tasks exist, yet none of their directories does. With no tasks at
    all there is nothing to read, and saying "available" costs nothing.
    """
    if not _reachable(artifacts_root, "dir"):
        return False
    dirs = conn.execute(text("SELECT artifact_dir FROM tasks")).scalars().all()
    if not dirs:
        return True
    return any(d and _reachable(Path(d), "dir") for d in dirs)


def _existing_prompt_results(results: Any) -> list[dict]:
    """The prompt results whose file is still on disk.

    Same rule as cloning a task: an entry without a path (a failed prompt) or
    whose file is gone has nothing for the recording to show.
    """
    kept: list[dict] = []
    for entry in results if isinstance(results, list) else []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str) and path and _reachable(Path(path), "file"):
            kept.append(dict(entry))
    return kept


def _create_missing(conn: Any) -> int:
    tasks = conn.execute(text(
        "SELECT t.id, t.user_id, t.source_url, t.source_title, t.status, t.options, "
        "t.artifact_dir, t.transcript_path, t.summary_path, t.created_at "
        "FROM tasks t LEFT JOIN recordings r ON r.source_task_id = t.id "
        "WHERE r.id IS NULL"
    )).mappings().all()
    if not tasks:
        return 0
    durations = dict(conn.execute(text(
        "SELECT task_id, MAX(end_sec) FROM asr_segments GROUP BY task_id"
    )).all())
    now = utcnow()
    for task in tasks:
        options = task["options"] if isinstance(task["options"], dict) else {}
        results = options.get("prompt_results")
        meta = {
            "prompt_results": [dict(r) for r in results if isinstance(r, dict)]
            if isinstance(results, list) else []
        }
        duration = durations.get(task["id"])
        state = initial_state(
            TaskStatus(task["status"]),
            transcript_path=task["transcript_path"],
            summary_path=task["summary_path"],
            prompt_results=meta["prompt_results"],
            options=options,
        )
        conn.execute(
            text(
                "INSERT INTO recordings (id, user_id, source_task_id, title, title_is_custom, "
                "source_url, artifact_dir, transcript_path, summary_path, state, duration_sec, "
                "language, tags, meta, recorded_at, created_at, updated_at) VALUES ("
                ":id, :user_id, :task_id, :title, false, :source_url, :artifact_dir, "
                ":transcript_path, :summary_path, :state, :duration, :language, "
                "CAST(:tags AS json), CAST(:meta AS json), :recorded_at, :created_at, :now)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": task["user_id"],
                "task_id": task["id"],
                "title": recording_display_name(task["source_title"], task["source_url"]),
                "source_url": task["source_url"],
                "artifact_dir": task["artifact_dir"],
                "transcript_path": task["transcript_path"],
                "summary_path": task["summary_path"],
                "state": state.value,
                "duration": float(duration) if duration is not None else None,
                "language": language_code(
                    options.get("language") or options.get("detected_language")
                ),
                "tags": "[]",
                "meta": json.dumps(meta),
                "recorded_at": task["created_at"],
                # The task's own creation time, as create_task would have
                # given the recording; the migration's clock would date every
                # backfilled recording identically (vts-lzad).
                "created_at": task["created_at"],
                "now": now,
            },
        )
    return len(tasks)


def _fill_content(conn: Any) -> tuple[int, int]:
    """Set the content fields the live code has not written yet.

    Only empty values are filled: a recording whose task ran after the deploy
    already carries what the pipeline wrote, and that is the truth. Recordings
    without a task are skipped by the join; nothing says what their task had.
    """
    rows = conn.execute(text(
        "SELECT r.id, r.artifact_dir, r.redacted_path, r.prompt_results, "
        "t.options AS task_options "
        "FROM recordings r JOIN tasks t ON t.id = r.source_task_id"
    )).mappings().all()
    redacted = prompt_results = 0
    for row in rows:
        if row["redacted_path"] is None and row["artifact_dir"]:
            path = Path(row["artifact_dir"]) / REDACTED_RELATIVE
            if _reachable(path, "file"):
                conn.execute(
                    text("UPDATE recordings SET redacted_path = :path WHERE id = :id"),
                    {"path": str(path), "id": row["id"]},
                )
                redacted += 1
        if not row["prompt_results"]:
            options = row["task_options"] if isinstance(row["task_options"], dict) else {}
            kept = _existing_prompt_results(options.get("prompt_results"))
            if kept:
                # A whole new value, never an in-place change of the old one.
                conn.execute(
                    text(
                        "UPDATE recordings SET prompt_results = CAST(:results AS json) "
                        "WHERE id = :id"
                    ),
                    {"results": json.dumps(kept), "id": row["id"]},
                )
                prompt_results += 1
    return redacted, prompt_results


def backfill_recording_content(conn: Any, *, artifacts_available: bool) -> dict[str, int]:
    """Data migration: create missing recordings and fill their content fields.

    `conn` is a synchronous SQLAlchemy connection inside the migration's
    transaction. Returns how many rows each part changed.

    `artifacts_available` only decides whether a warning is printed. The fill
    always runs: it checks every file on its own absolute path, so without the
    volume it writes nothing anyway, while a root check that is wrong (the
    configured artifacts_root differing from where the volume is mounted)
    would otherwise skip the fill for good, since this migration runs once.
    """
    if not artifacts_available:
        print(
            "recording content: artifacts root not found; content fields filled "
            "only where files are reachable"
        )
    created = _create_missing(conn)
    redacted, prompt_results = _fill_content(conn)
    return {"created": created, "redacted": redacted, "prompt_results": prompt_results}


def backfill_listed_at(conn: Any) -> int:
    """Give every recording that already has text its place in the library order.

    `listed_at` is the moment a recording got text; for rows from before the
    column existed the best available answer is `created_at`. This also
    covers the recordings migration 0034 created, which runs before the
    column exists and so cannot set it. Only empty
    values are set, so a repeat changes nothing. Returns the rows changed.
    """
    result = conn.execute(text(
        "UPDATE recordings SET listed_at = created_at "
        "WHERE transcript_path IS NOT NULL AND listed_at IS NULL"
    ))
    return int(result.rowcount or 0)


def backfill_recording_states(conn: Any) -> int:
    """Set `state` on every recording; sync Connection, used by migration 0032.

    That migration runs before recordings have content columns of their own,
    so the content is judged from the task's old fields.
    """
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
            options = row["task_options"] if isinstance(row["task_options"], dict) else {}
            state = initial_state(
                TaskStatus(row["task_status"]),
                transcript_path=row["transcript_path"],
                summary_path=row["summary_path"],
                prompt_results=options.get("prompt_results"),
                options=options,
            )
        conn.execute(
            text("UPDATE recordings SET state = :state WHERE id = :id"),
            {"state": state.value, "id": row["id"]},
        )
    return len(rows)

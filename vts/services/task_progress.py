from __future__ import annotations

from vts.db.models import Task


def summary_progress_for_task(task: Task) -> tuple[int, int]:
    options = task.options if isinstance(task.options, dict) else {}
    if options.get("summary") is False:
        return (0, 0)
    prog = task.summary_progress
    if not isinstance(prog, dict):
        return (0, 0)
    current = prog.get("current", 0)
    total = prog.get("total", 0)
    return (max(int(current), 0), max(int(total), 0))

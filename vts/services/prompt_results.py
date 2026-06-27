from __future__ import annotations

from typing import Any

from vts.db.models import Task
from vts.services.prompt_registry import ref_key


def result_entries(task: Task) -> list[dict[str, Any]]:
    pr = task.options.get("prompt_results") if isinstance(task.options, dict) else None
    return pr if isinstance(pr, list) else []


def resolve_result_path(task: Task, source: str, ref: str) -> str | None:
    wanted = ref_key(source, ref)
    for entry in result_entries(task):
        if ref_key(str(entry.get("source")), str(entry.get("id"))) == wanted:
            path = entry.get("path")
            if isinstance(path, str) and path:
                return path
    if source == "system" and ref == "summary" and task.summary_path:
        return task.summary_path
    return None

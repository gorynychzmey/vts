from __future__ import annotations

from typing import Any

from vts.db.models import Task
from vts.services.prompt_registry import ref_key


def upsert_result_entry(
    options: dict, source: str, id: str, name: str, path: str, status: str
) -> list[dict[str, Any]]:
    """Insert or update a prompt-result entry inside ``options['prompt_results']``.

    Returns the (possibly newly created) ``prompt_results`` list so the caller can
    hand it to ``Repo.set_task_prompt_results`` for a JSON-column-safe write-back.
    """
    entries = options.setdefault("prompt_results", [])
    if not isinstance(entries, list):
        entries = []
        options["prompt_results"] = entries
    target = ref_key(source, id)
    for entry in entries:
        if ref_key(str(entry.get("source")), str(entry.get("id"))) == target:
            entry.update(name=name, path=path, status=status)
            return entries
    entries.append(
        {"source": source, "id": id, "name": name, "path": path, "status": status}
    )
    return entries


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

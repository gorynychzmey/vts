from __future__ import annotations

from pathlib import Path
from typing import Any

from vts.db.models import Task
from vts.services.prompt_registry import ref_key


def upsert_result_entry(
    options: dict, source: str, id: str, name: str, path: str, status: str
) -> list[dict[str, Any]]:
    """Insert or update a prompt-result entry inside ``options['prompt_results']``.

    Returns a FRESH ``prompt_results`` list (with copied entry dicts) so the
    caller can hand it to ``Repo.set_task_prompt_results`` for a JSON-column-safe
    write-back. The input ``options`` and any existing list/entries are left
    unmutated: callers pass a shallow ``dict(task.options)`` whose
    ``prompt_results`` list is the same object SQLAlchemy loaded for the JSON
    column. Mutating it in place is not change-tracked, so a subsequent commit
    silently drops the write (e.g. the second of two finalize steps). Building a
    new list guarantees the reassignment is detected and persisted.
    """
    existing = options.get("prompt_results")
    entries: list[dict[str, Any]] = (
        [dict(e) for e in existing] if isinstance(existing, list) else []
    )
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


def downgrade_system_summary_entry(task) -> None:
    """Mark the system:summary entry in ``prompt_results`` as pending.

    Summary restarts delete the final.md/summary.md files but keep the other
    finalize results; a completed system:summary entry would then point at a
    deleted file and the UI would select it and hit 404 (vts-b6l). Reassigns
    task.options so the JSON column persists on commit. No-op when the entry
    is absent or not completed.
    """
    existing = result_entries(task)
    wanted = ref_key("system", "summary")
    changed = False
    entries: list[dict[str, Any]] = []
    for e in existing:
        e = dict(e)
        if (
            ref_key(str(e.get("source")), str(e.get("id"))) == wanted
            and e.get("status") == "completed"
        ):
            e["status"] = "pending"
            changed = True
        entries.append(e)
    if not changed:
        return
    new_options = dict(task.options or {})
    new_options["prompt_results"] = entries
    task.options = new_options


def clear_all_finalize_results(task) -> None:
    """Delete every finalize result file and reset the prompt_results index.

    Removes custom result files (summary/results/*), the system summary
    (summary/final.* + outputs/summary.*), empties options['prompt_results'],
    and clears task.summary_path. Reassigns task.options so the JSON column
    persists on commit.
    """
    artifact_root = Path(task.artifact_dir) if task.artifact_dir else None
    if artifact_root is not None:
        summary_dir = artifact_root / "summary"
        outputs_dir = artifact_root / "outputs"
        # custom result files
        results_dir = summary_dir / "results"
        if results_dir.exists():
            for p in results_dir.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        # system summary files
        for p in (summary_dir / "final.md", summary_dir / "final.json",
                  outputs_dir / "summary.md", outputs_dir / "summary.json"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    new_options = dict(task.options or {})
    new_options["prompt_results"] = []
    task.options = new_options
    task.summary_path = None

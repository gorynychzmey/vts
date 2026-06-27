from __future__ import annotations

from vts.db.models import Task
from vts.services.prompt_registry import parse_ref, ref_to_dict


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


def selected_prompt_refs(options: dict) -> list[dict]:
    if isinstance(options, dict) and isinstance(options.get("prompts"), list):
        refs: list[dict] = []
        for entry in options["prompts"]:
            try:
                source, ref_id = parse_ref(entry)
            except (ValueError, TypeError):
                continue
            refs.append(ref_to_dict(source, ref_id))
        return refs
    summary = options.get("summary", True) if isinstance(options, dict) else True
    if summary is False:
        return []
    return [ref_to_dict("system", "summary")]

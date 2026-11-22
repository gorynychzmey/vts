from __future__ import annotations

from pathlib import Path
from typing import Any

from vts.services.prompt_registry import ref_key


def _copied(entries: Any) -> list[dict[str, Any]]:
    """A fresh list of fresh dicts, so the caller can reassign a JSON column."""
    return [dict(e) for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def upsert_result_entry(
    entries: Any, source: str, id: str, name: str, path: str, status: str
) -> list[dict[str, Any]]:
    """Insert or update one entry of a recording's prompt-result index.

    Returns a FRESH list (with copied entry dicts) for
    ``Repo.set_task_prompt_results``; ``entries`` is left unmutated. It is the
    very list SQLAlchemy loaded for the JSON column, and an in-place change is
    not change-tracked, so a later commit would silently drop the write (e.g.
    the second of two finalize steps).
    """
    result = _copied(entries)
    target = ref_key(source, id)
    for entry in result:
        if ref_key(str(entry.get("source")), str(entry.get("id"))) == target:
            entry.update(name=name, path=path, status=status)
            return result
    result.append(
        {"source": source, "id": id, "name": name, "path": path, "status": status}
    )
    return result


def _downgrade_entries(entries: Any, should_downgrade) -> list[dict[str, Any]]:
    """A copy of ``entries`` with the matching completed ones set to pending."""
    result = _copied(entries)
    for e in result:
        if e.get("status") == "completed" and should_downgrade(e):
            e["status"] = "pending"
    return result


def downgrade_system_summary_entry(entries: Any) -> list[dict[str, Any]]:
    """Mark the system:summary entry as pending.

    Final-only summary restarts delete the final.md/summary.md files but keep
    the other finalize results; a completed system:summary entry would then
    point at a deleted file and the UI would select it and hit 404 (vts-b6l).
    """
    wanted = ref_key("system", "summary")
    return _downgrade_entries(
        entries,
        lambda e: ref_key(str(e.get("source")), str(e.get("id"))) == wanted,
    )


def downgrade_all_result_entries(entries: Any) -> list[dict[str, Any]]:
    """Mark every completed entry as pending.

    Full summary restarts regenerate the processed transcript, so every
    finalize result — the system summary and user prompts alike — is stale
    and about to be re-run (vts-5eg).
    """
    return _downgrade_entries(entries, lambda e: True)


def clear_all_finalize_results(artifact_dir: str | None) -> list[dict[str, Any]]:
    """Delete every finalize result file; return the emptied result index.

    Removes custom result files (summary/results/*) and the system summary
    (summary/final.* + outputs/summary.*). The caller writes the returned
    (empty) index and clears the summary path through the repo setters.
    """
    artifact_root = Path(artifact_dir) if artifact_dir else None
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
            except OSError:
                pass
    return []

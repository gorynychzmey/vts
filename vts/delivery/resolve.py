"""Resolve delivery variant content from task artifacts.

Mirrors how the MCP layer reads transcript/summary files
(see vts/mcp/tools.py: get_transcript).
"""
from __future__ import annotations

from pathlib import Path

from vts.delivery.contract import DeliveryPayload, TaskMeta

VALID_VARIANTS = ("raw", "redacted", "summary")


class VariantUnavailable(RuntimeError):
    """The requested variant has no content for this task."""


def _task_meta(task) -> TaskMeta:
    opts = task.options or {}
    return TaskMeta(
        source_url=task.source_url,
        source_title=task.source_title,
        language=opts.get("detected_language") or opts.get("language"),
        duration_s=opts.get("duration_s"),
        created_at=task.created_at,
    )


def _read(path: Path | None, *, variant: str) -> str:
    if path is None:
        raise VariantUnavailable(f"{variant}: no path recorded")
    if not path.exists():
        raise VariantUnavailable(f"{variant}: file missing at {path}")
    return path.read_text(encoding="utf-8")


def resolve_variant(task, variant: str) -> DeliveryPayload:
    if variant not in VALID_VARIANTS:
        raise VariantUnavailable(f"unknown variant: {variant}")

    if variant == "raw":
        path = Path(task.transcript_path) if task.transcript_path else None
        content = _read(path, variant=variant)
        fmt = "txt" if path.suffix == ".txt" else "json"
    elif variant == "summary":
        path = Path(task.summary_path) if task.summary_path else None
        content = _read(path, variant=variant)
        fmt = "markdown"
    else:  # redacted
        path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
        content = _read(path, variant=variant)
        fmt = "txt"

    return DeliveryPayload(
        task_id=str(task.id),
        variant=variant,
        content=content,
        content_format=fmt,
        task=_task_meta(task),
    )

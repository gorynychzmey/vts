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


def _resolve_prompt_variant(task, variant: str) -> DeliveryPayload:
    """Deliver the rendered output of one prompt run against this task.

    Reuses the addressing the MCP/REST result endpoints already use, so a
    delivery names a prompt result exactly the way everything else does.
    """
    from vts.services.prompt_registry import parse_ref
    from vts.services.prompt_results import resolve_result_path

    try:
        source, ref_id = parse_ref(variant)
    except ValueError as exc:
        raise VariantUnavailable(f"unknown variant: {variant}") from exc

    path = resolve_result_path(task, source, ref_id)
    if not path:
        # Not an error the pipeline should die on: the prompt may simply not
        # have run for this task (it was deselected, or the task predates the
        # prompt). VariantUnavailable is what the consumer already handles.
        raise VariantUnavailable(f"{variant}: no result recorded for this task")

    content = _read(Path(path), variant=variant)
    return DeliveryPayload(
        task_id=str(task.id),
        variant=variant,
        content=content,
        # Prompt results are rendered by an LLM against a markdown-shaped
        # instruction, the same as the summary variant.
        content_format="markdown",
        task=_task_meta(task),
    )


def is_prompt_variant(variant: str) -> bool:
    """True when `variant` addresses a prompt result rather than a fixed file.

    The three fixed variants are bare words; a prompt result is addressed the
    way the rest of the codebase already addresses one — "source:id", e.g.
    "user:<uuid>" (vts-as1i).
    """
    return isinstance(variant, str) and ":" in variant


def resolve_variant(task, variant: str) -> DeliveryPayload:
    if is_prompt_variant(variant):
        return _resolve_prompt_variant(task, variant)
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

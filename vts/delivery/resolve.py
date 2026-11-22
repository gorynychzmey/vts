"""Resolve delivery variant content from a task's recording.

The recording owns the content (vts-lzad); the task only addresses the
delivery. Reads go through the same layer as the MCP tools
(see vts/services/recording_artifacts.py).
"""
from __future__ import annotations

from vts.delivery.contract import DeliveryPayload, TaskMeta
from vts.services.recording_artifacts import (
    RecordingArtifactMissing,
    _artifact_path,
    read_recording_result,
    read_recording_transcript,
)
from vts.services.recording_meta import language_code

VALID_VARIANTS = ("raw", "redacted", "summary")


class VariantUnavailable(RuntimeError):
    """The requested variant has no content for this task."""


def _task_meta(task, recording, tags=()) -> TaskMeta:
    opts = task.options or {}
    return TaskMeta(
        source_url=task.source_url,
        source_title=task.source_title,
        # The recording's columns first; the task's options only for a
        # recording whose language was never set — normalized like the column,
        # since the contract promises an ISO code ("ru"), not "Russian".
        language=language_code(
            recording.language or opts.get("detected_language") or opts.get("language")
        ),
        duration_s=recording.duration_sec,
        created_at=task.created_at,
        # Sorted here as well as by the query: the contract promises an order,
        # and a caller passing tags in any order must not break it.
        tags=tuple(t.name for t in sorted(tags, key=lambda t: t.name_key)),
    )


def _resolve_prompt_variant(task, variant: str, recording, tags=()) -> DeliveryPayload:
    """Deliver the rendered output of one prompt run against this recording.

    Reuses the addressing the MCP/REST result endpoints already use, so a
    delivery names a prompt result exactly the way everything else does.
    """
    from vts.services.prompt_registry import parse_ref

    try:
        source, ref_id = parse_ref(variant)
    except ValueError as exc:
        raise VariantUnavailable(f"unknown variant: {variant}") from exc

    # Not an error the pipeline should die on: the prompt may simply not have
    # run for this recording (it was deselected, or it predates the prompt).
    # VariantUnavailable is what the consumer already handles.
    try:
        content = read_recording_result(recording, source, ref_id)
    except RecordingArtifactMissing as exc:
        raise VariantUnavailable(f"{variant}: {exc}") from exc
    return DeliveryPayload(
        task_id=str(task.id),
        variant=variant,
        content=content,
        # Prompt results are rendered by an LLM against a markdown-shaped
        # instruction, the same as the summary variant.
        content_format="markdown",
        task=_task_meta(task, recording, tags),
    )


def is_prompt_variant(variant: str) -> bool:
    """True when `variant` addresses a prompt result rather than a fixed file.

    The three fixed variants are bare words; a prompt result is addressed the
    way the rest of the codebase already addresses one — "source:id", e.g.
    "user:<uuid>" (vts-as1i).
    """
    return isinstance(variant, str) and ":" in variant


def resolve_variant(task, variant: str, recording, tags=()) -> DeliveryPayload:
    """`tags` are the recording's Tag rows (anything with `name` and
    `name_key`), loaded by the caller: resolving stays free of database access."""
    if is_prompt_variant(variant):
        if recording is None:
            raise VariantUnavailable(f"{variant}: the task has no recording")
        return _resolve_prompt_variant(task, variant, recording, tags)
    if variant not in VALID_VARIANTS:
        raise VariantUnavailable(f"unknown variant: {variant}")
    if recording is None:
        raise VariantUnavailable(f"{variant}: the task has no recording")

    try:
        content = read_recording_transcript(recording, variant)
    except RecordingArtifactMissing as exc:
        raise VariantUnavailable(f"{variant}: {exc}") from exc
    if variant == "raw":
        raw_path = _artifact_path(recording, "raw")
        fmt = "txt" if raw_path is not None and raw_path.suffix == ".txt" else "json"
    elif variant == "summary":
        fmt = "markdown"
    else:  # redacted
        fmt = "txt"

    return DeliveryPayload(
        task_id=str(task.id),
        variant=variant,
        content=content,
        content_format=fmt,
        task=_task_meta(task, recording, tags),
    )

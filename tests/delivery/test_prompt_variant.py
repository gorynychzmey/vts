"""Delivering the result of a user prompt (vts-as1i).

Delivery used to be limited to three fixed artifacts, so the output of a
user's own prompt — often the thing they actually care about — could not be
sent anywhere. `variant` now also accepts a prompt ref ("source:id"), reusing
the addressing the result endpoints already use.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vts.delivery.resolve import (
    VariantUnavailable,
    is_prompt_variant,
    resolve_variant,
)


def _task(tmp_path):
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_url="http://v/1",
        source_title="Meeting",
        created_at=datetime.now(timezone.utc),
        artifact_dir=str(tmp_path),
        transcript_path=None,
        summary_path=None,
        options={},
    )


def _recording(tmp_path, *, prompt_results=None, summary=None):
    return SimpleNamespace(
        artifact_dir=str(tmp_path),
        transcript_path=None,
        summary_path=summary,
        redacted_path=None,
        prompt_results=prompt_results or [],
        language=None,
        duration_sec=None,
    )


def test_is_prompt_variant_distinguishes_refs_from_fixed_words():
    assert is_prompt_variant("user:abc")
    assert is_prompt_variant("system:summary")
    for fixed in ("raw", "redacted", "summary"):
        assert not is_prompt_variant(fixed)


def test_delivers_a_user_prompt_result(tmp_path):
    out = tmp_path / "memo.md"
    out.write_text("# Action items\n- ship it", encoding="utf-8")
    ref = f"user:{uuid.uuid4()}"
    task = _task(tmp_path)
    recording = _recording(tmp_path, prompt_results=[
        {"source": "user", "id": ref.split(":", 1)[1], "name": "Memo",
         "path": str(out), "status": "completed"},
    ])

    payload = resolve_variant(task, ref, recording)
    assert payload.content == "# Action items\n- ship it"
    assert payload.variant == ref
    assert payload.content_format == "markdown"
    assert payload.task.source_title == "Meeting"


def test_missing_result_raises_variant_unavailable_not_a_crash(tmp_path):
    """The prompt may simply not have run for this task (deselected, or the
    task predates the prompt). The consumer already handles this exception;
    anything else would surface as a hard delivery failure."""
    task = _task(tmp_path)
    with pytest.raises(VariantUnavailable):
        resolve_variant(task, f"user:{uuid.uuid4()}", _recording(tmp_path))


def test_recorded_result_whose_file_vanished_raises_variant_unavailable(tmp_path):
    ref_id = str(uuid.uuid4())
    task = _task(tmp_path)
    recording = _recording(tmp_path, prompt_results=[
        {"source": "user", "id": ref_id, "name": "Memo",
         "path": str(tmp_path / "gone.md"), "status": "completed"},
    ])
    with pytest.raises(VariantUnavailable):
        resolve_variant(task, f"user:{ref_id}", recording)


def test_malformed_ref_is_rejected(tmp_path):
    task = _task(tmp_path)
    recording = _recording(tmp_path)
    with pytest.raises(VariantUnavailable):
        resolve_variant(task, "nonsense:", recording)
    with pytest.raises(VariantUnavailable):
        resolve_variant(task, "bogus-source:x", recording)


def test_system_summary_ref_falls_back_to_summary_path(tmp_path):
    """resolve_result_path already treats system:summary specially; delivery
    inherits that rather than reimplementing it."""
    summary = tmp_path / "summary.md"
    summary.write_text("the gist", encoding="utf-8")
    task = _task(tmp_path)
    recording = _recording(tmp_path, summary=str(summary))

    payload = resolve_variant(task, "system:summary", recording)
    assert payload.content == "the gist"


def test_fixed_variants_still_work(tmp_path):
    """The three original variants are untouched by the extension."""
    redacted = tmp_path / "outputs" / "redacted_transcript.txt"
    redacted.parent.mkdir(parents=True, exist_ok=True)
    redacted.write_text("polished", encoding="utf-8")
    task = _task(tmp_path)
    recording = _recording(tmp_path)

    payload = resolve_variant(task, "redacted", recording)
    assert payload.content == "polished"
    assert payload.content_format == "txt"

    with pytest.raises(VariantUnavailable):
        resolve_variant(task, "not-a-variant", recording)


def test_a_result_only_the_task_lists_is_not_delivered(tmp_path):
    """Task and recording disagree on purpose: the recording's list decides."""
    out = tmp_path / "memo.md"
    out.write_text("task-only memo", encoding="utf-8")
    ref_id = str(uuid.uuid4())
    task = _task(tmp_path)
    task.options = {"prompt_results": [
        {"source": "user", "id": ref_id, "path": str(out), "status": "completed"},
    ]}
    with pytest.raises(VariantUnavailable):
        resolve_variant(task, f"user:{ref_id}", _recording(tmp_path))

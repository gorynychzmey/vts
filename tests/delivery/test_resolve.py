import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vts.delivery.resolve import resolve_variant, VariantUnavailable


def _fake_task(tmp_path, **over):
    t = types.SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        source_url="http://x", source_title="Title",
        artifact_dir=str(tmp_path),
        transcript_path=None, summary_path=None,
        options={"detected_language": "en"},
        created_at=datetime.now(timezone.utc),
    )
    for k, v in over.items():
        setattr(t, k, v)
    return t


def _fake_recording(tmp_path, **over):
    r = types.SimpleNamespace(
        artifact_dir=str(tmp_path),
        transcript_path=None, summary_path=None, redacted_path=None,
        prompt_results=[], language="en", duration_sec=None,
    )
    for k, v in over.items():
        setattr(r, k, v)
    return r


def test_raw_reads_the_recordings_transcript(tmp_path):
    p = tmp_path / "transcript.txt"; p.write_text("hello raw")
    t = _fake_task(tmp_path)
    r = _fake_recording(tmp_path, transcript_path=str(p))
    payload = resolve_variant(t, "raw", r)
    assert payload.content == "hello raw"
    assert payload.variant == "raw"
    assert payload.content_format == "txt"
    assert payload.task.language == "en"


def test_summary_reads_the_recordings_summary(tmp_path):
    p = tmp_path / "summary.md"; p.write_text("# sum")
    r = _fake_recording(tmp_path, summary_path=str(p))
    assert resolve_variant(_fake_task(tmp_path), "summary", r).content == "# sum"


def test_task_and_recording_disagree_the_recording_wins(tmp_path):
    stale = tmp_path / "stale.txt"; stale.write_text("task copy")
    fresh = tmp_path / "fresh.txt"; fresh.write_text("recording copy")
    t = _fake_task(
        tmp_path, transcript_path=str(stale),
        options={"detected_language": "de", "duration_s": 1.0},
    )
    r = _fake_recording(tmp_path, transcript_path=str(fresh), language="fr", duration_sec=99.5)
    payload = resolve_variant(t, "raw", r)
    assert payload.content == "recording copy"
    assert payload.task.language == "fr"
    assert payload.task.duration_s == 99.5


def test_missing_raises(tmp_path):
    t = _fake_task(tmp_path)
    r = _fake_recording(tmp_path)
    with pytest.raises(VariantUnavailable):
        resolve_variant(t, "raw", r)


def test_no_recording_raises_for_every_variant(tmp_path):
    p = tmp_path / "transcript.txt"; p.write_text("task only")
    t = _fake_task(tmp_path, transcript_path=str(p))
    for variant in ("raw", "redacted", "summary", "user:abc", "system:summary"):
        with pytest.raises(VariantUnavailable):
            resolve_variant(t, variant, None)


@pytest.mark.parametrize("variant,default_name", [("raw", "transcript.txt"), ("summary", "summary.md")])
def test_a_null_stored_path_is_unavailable_even_with_a_default_named_file(tmp_path, variant, default_name):
    """vts-lzad: the same answer as HTTP — a NULL path is "not ready", and a
    stale default-named file (e.g. during a summary restart) is not delivered."""
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / default_name).write_text("stale")
    with pytest.raises(VariantUnavailable):
        resolve_variant(_fake_task(tmp_path), variant, _fake_recording(tmp_path))


def test_language_is_an_iso_code_even_from_the_task_fallback(tmp_path):
    """TaskMeta.language is documented as a normalized ISO code (vts-lzad)."""
    p = tmp_path / "transcript.txt"; p.write_text("x")
    t = _fake_task(tmp_path, options={"detected_language": "Russian"})
    r = _fake_recording(tmp_path, transcript_path=str(p), language=None)
    assert resolve_variant(t, "raw", r).task.language == "ru"

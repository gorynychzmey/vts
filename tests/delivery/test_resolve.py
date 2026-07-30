import types
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
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    for k, v in over.items():
        setattr(t, k, v)
    return t


def test_raw_reads_transcript_path(tmp_path):
    p = tmp_path / "transcript.txt"; p.write_text("hello raw")
    t = _fake_task(tmp_path, transcript_path=str(p))
    payload = resolve_variant(t, "raw")
    assert payload.content == "hello raw"
    assert payload.variant == "raw"
    assert payload.task.language == "en"


def test_summary_reads_summary_path(tmp_path):
    p = tmp_path / "summary.md"; p.write_text("# sum")
    t = _fake_task(tmp_path, summary_path=str(p))
    assert resolve_variant(t, "summary").content == "# sum"


def test_missing_raises(tmp_path):
    t = _fake_task(tmp_path, transcript_path=None)
    with pytest.raises(VariantUnavailable):
        resolve_variant(t, "raw")

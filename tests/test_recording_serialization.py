"""The has_* flags a library row carries, and where they must NOT look.

They are probed from disk rather than stored, because archiving removes files
and a stored flag would go stale (see `_serialize`). Probing has its own trap:
a recording with no artifact directory must answer "no", not answer from
whatever directory the server process happens to be running in.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from vts.api.routers.recordings import _serialize
from vts.db.models import Recording, RecordingState


def _recording(**kw) -> Recording:
    now = datetime.now(tz=timezone.utc)
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Call",
        title_is_custom=False,
        source_url="file://call.m4a",
        artifact_dir="",
        transcript_path=None,
        summary_path=None,
        state=RecordingState.processing,
        meta={},
        tags=[],
        created_at=now,
        updated_at=now,
    )
    defaults.update(kw)
    return Recording(**defaults)


def test_a_recording_without_an_artifact_dir_reports_no_redacted_transcript(
    tmp_path: Path, monkeypatch,
) -> None:
    """vts-6wvy: `Path("")` is `Path(".")`, so the probe fell back to the CWD.

    An empty artifact_dir made the check read
    `./outputs/redacted_transcript.txt` — relative to wherever the process was
    started. Practically always False, but the value came from the working
    directory rather than from the recording, which is the kind of dependency
    that answers differently in a container, in a test and under systemd.

    Set up so the CWD would answer "yes" if it were consulted at all.
    """
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "redacted_transcript.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    payload = _serialize(_recording(artifact_dir=""))

    assert payload.has_redacted is False
    # The sibling flags are unaffected and must stay honest.
    assert payload.has_transcript is False
    assert payload.has_summary is False
    assert payload.has_media is False


def test_a_real_artifact_dir_is_still_probed(tmp_path: Path) -> None:
    """The counterpart: refusing an empty dir must not refuse a real one."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "redacted_transcript.txt").write_text("redacted", encoding="utf-8")

    payload = _serialize(_recording(artifact_dir=str(tmp_path)))

    assert payload.has_redacted is True


def test_serialized_recording_carries_state() -> None:
    """vts-lzad: the library tells a client whether the text is final."""
    for state in RecordingState:
        assert _serialize(_recording(state=state)).state == state.value


def test_prompt_results_and_redacted_flag_come_from_the_recording_columns(tmp_path: Path) -> None:
    """vts-lzad: the column is the source; meta is a stale legacy snapshot."""
    custom = tmp_path / "elsewhere.txt"
    custom.write_text("redacted")
    memo = tmp_path / "summary" / "results" / "user__m.md"
    memo.parent.mkdir(parents=True)
    memo.write_text("memo")
    real = {"source": "user", "id": "m", "name": "Memo", "path": str(memo), "status": "completed"}
    rec = _recording(
        artifact_dir=str(tmp_path),
        redacted_path=str(custom),
        prompt_results=[real],
        meta={"prompt_results": [{"source": "user", "id": "stale"}]},
    )
    out = _serialize(rec)
    assert out.prompt_results == [real]
    # The default-named file does not exist; the column points elsewhere.
    assert out.has_redacted is True


def test_prompt_results_list_only_results_whose_file_is_there(tmp_path: Path) -> None:
    """vts-lzad: a result is offered only if it can be read. A Memo whose file
    vanished (or a pending entry with no file yet) would open as a 404."""
    kept = tmp_path / "summary" / "results" / "user__kept.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("kept")
    present = {"source": "user", "id": "kept", "name": "Kept", "path": str(kept), "status": "completed"}
    rec = _recording(
        artifact_dir=str(tmp_path),
        prompt_results=[
            present,
            {"source": "user", "id": "gone", "name": "Memo",
             "path": str(tmp_path / "summary" / "results" / "user__gone.md"), "status": "completed"},
            {"source": "user", "id": "next", "name": "Next", "status": "pending"},
        ],
    )
    assert _serialize(rec).prompt_results == [present]

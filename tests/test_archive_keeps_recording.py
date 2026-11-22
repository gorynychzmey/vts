"""Archiving a task must keep what the recording owns (vts-lzad).

The task and its recording share one artifact directory, so archive cannot be
allowed to treat the transcript pair as the whole result: the player, the
processed transcript and the prompt results all read from the same directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from vts.api._helpers.artifact_store import (
    ARCHIVED_LOG_MESSAGE,
    RECORDING_OWNED_RELPATHS,
    _archive_task_artifacts,
    player_blocks_from_artifacts,
)
from vts.services.recording_artifacts import (
    read_recording_transcript,
    recording_transcript_entries,
)

_TRANSCRIPT_JSON = {
    "entries": [
        {"start": 0.0, "end": 2.0, "text": "Hello there.", "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "text": "General Kenobi.", "speaker": "SPEAKER_01"},
    ]
}
_SEGMENTS_RAW = {
    "segments": [
        {
            "offset": 0.0,
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Hello there."},
                {"start": 2.0, "end": 4.0, "text": "General Kenobi."},
            ],
        }
    ]
}


def _write(root: Path, rel: str, content: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _populate(root: Path) -> None:
    for rel in RECORDING_OWNED_RELPATHS:
        if rel == "outputs/transcript.json":
            _write(root, rel, json.dumps(_TRANSCRIPT_JSON))
        elif rel == "asr/segments_raw.json":
            _write(root, rel, json.dumps(_SEGMENTS_RAW))
        else:
            _write(root, rel)
    _write(root, "summary/results/user__p1.md", "result one")
    _write(root, "summary/results/system__p2.md", "result two")


def test_archive_keeps_every_recording_owned_file(tmp_path: Path) -> None:
    _populate(tmp_path)
    doomed = [
        "media/audio_16k.wav",
        "outputs/diarization.json",
        "outputs/speaker_matches.json",
        "summary/windows.json",
        "language_detection.json",
    ]
    for rel in doomed:
        _write(tmp_path, rel)
    _write(tmp_path, "logs/task.log", "old log")
    task = SimpleNamespace(artifact_dir=str(tmp_path))

    _archive_task_artifacts(task, None)

    survivors = {
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    }
    expected = set(RECORDING_OWNED_RELPATHS) | {
        "summary/results/user__p1.md",
        "summary/results/system__p2.md",
        "logs/task.log",
    }
    assert survivors == expected
    assert (tmp_path / "logs" / "task.log").read_text().strip() == ARCHIVED_LOG_MESSAGE


def test_recording_readers_survive_archive(tmp_path: Path) -> None:
    _populate(tmp_path)
    _write(tmp_path, "outputs/redacted_transcript.txt", "redacted body")
    _write(tmp_path, "media/audio_16k.wav")
    recording = SimpleNamespace(
        artifact_dir=str(tmp_path), transcript_path=None, summary_path=None
    )
    task = SimpleNamespace(artifact_dir=str(tmp_path))

    _archive_task_artifacts(task, recording)

    assert read_recording_transcript(recording, "redacted") == "redacted body"
    assert len(recording_transcript_entries(recording)) == 2
    blocks = player_blocks_from_artifacts(
        str(tmp_path), language="en", names={}
    )
    assert blocks
    assert (tmp_path / "summary" / "results" / "user__p1.md").read_text() == "result one"


def test_archive_keeps_the_paths_the_recording_stores(tmp_path: Path) -> None:
    """The recording's own stored paths survive; a file nobody stores does not."""
    kept = _write(tmp_path, "outputs/custom_transcript.txt", "recording's")
    unstored = _write(tmp_path, "outputs/old_summary.md", "nobody's")
    task = SimpleNamespace(artifact_dir=str(tmp_path))
    recording = SimpleNamespace(transcript_path=str(kept), summary_path=None)

    _archive_task_artifacts(task, recording)

    assert kept.exists()
    assert not unstored.exists()

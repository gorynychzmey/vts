"""Reading a recording's artifacts without going through its task (vts-lib3).

Search returns `recording_id` because that is the identifier which lasts — and
until now there was nothing to fetch with it: every artifact route and every MCP
tool took a `task_id`. The one identifier clients are told to keep could not be
used to read anything, and a recording whose task had been deleted was
unreachable even though its files were sitting on disk.

A recording carries its own `artifact_dir`, so reading needs no task at all.
That is the point of the split: the recording is the lasting object, the task
was a job that produced it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

# What a caller can ask for. Deliberately named after what they ARE rather than
# after the files, since the on-disk layout is an implementation detail.
ArtifactKind = Literal["raw", "redacted", "summary"]

_RELATIVE_PATHS: dict[str, str] = {
    "raw": "outputs/transcript.txt",
    "redacted": "outputs/redacted_transcript.txt",
    "summary": "outputs/summary.md",
}


class RecordingArtifactMissing(FileNotFoundError):
    """The recording has no such artifact — archived away, or never produced."""


def _artifact_path(recording: Any, kind: str) -> Path:
    # The stored path wins when there is one: a task may have written its
    # transcript somewhere other than the default name.
    if kind == "raw" and getattr(recording, "transcript_path", None):
        return Path(recording.transcript_path)
    if kind == "summary" and getattr(recording, "summary_path", None):
        return Path(recording.summary_path)
    root = Path(str(getattr(recording, "artifact_dir", "") or ""))
    return root / _RELATIVE_PATHS.get(kind, _RELATIVE_PATHS["raw"])


def read_recording_transcript(recording: Any, kind: ArtifactKind = "raw") -> str:
    """One text artifact of a recording.

    Raises RecordingArtifactMissing rather than returning "" — an empty string
    reads as "the recording says nothing", which is a different claim from "this
    artifact was never produced or has been archived away".
    """
    path = _artifact_path(recording, kind)
    if not path.exists():
        raise RecordingArtifactMissing(
            f"recording has no {kind} artifact ({path.name})"
        )
    return path.read_text(encoding="utf-8")


def recording_transcript_entries(
    recording: Any,
    *,
    around_sec: float | None = None,
    window_sec: float = 60.0,
) -> list[dict[str, Any]]:
    """The transcript as timed entries — `{start, end, text, speaker}`.

    Flat text cannot be cited: an assistant that found a passage in it has no
    way to say when it was said. These entries are what the player already uses,
    and they are what makes a quote checkable.

    With `around_sec` only the entries overlapping that window are returned.
    A search hit points at a second, and the useful answer is the passage AROUND
    it — returning a two-hour transcript to show one quote wastes the client's
    context and buries the part that mattered.
    """
    root = Path(str(getattr(recording, "artifact_dir", "") or ""))
    path = root / "outputs" / "transcript.json"
    if not path.exists():
        raise RecordingArtifactMissing("recording has no structured transcript")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordingArtifactMissing("structured transcript is unreadable") from exc

    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    entries = [e for e in (raw_entries or []) if isinstance(e, dict)]
    if around_sec is None:
        return entries

    low = float(around_sec) - float(window_sec)
    high = float(around_sec) + float(window_sec)
    windowed: list[dict[str, Any]] = []
    for entry in entries:
        try:
            start = float(entry.get("start"))
            end = float(entry.get("end"))
        except (TypeError, ValueError):
            continue
        # Overlap, not containment: an entry straddling the edge of the window
        # is part of the passage a reader needs.
        if end >= low and start <= high:
            windowed.append(entry)
    return windowed

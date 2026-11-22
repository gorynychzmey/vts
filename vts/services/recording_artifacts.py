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
import logging
from pathlib import Path
from typing import Any, Literal

# What a caller can ask for. Deliberately named after what they ARE rather than
# after the files, since the on-disk layout is an implementation detail.
ArtifactKind = Literal["raw", "redacted", "summary"]

# Where a legacy row's redacted transcript sits when `redacted_path` is NULL.
_DEFAULT_REDACTED_RELPATH = "outputs/redacted_transcript.txt"


logger = logging.getLogger(__name__)


class RecordingArtifactMissing(FileNotFoundError):
    """The recording has no such artifact — archived away, or never produced."""


def _artifact_path(recording: Any, kind: str) -> Path | None:
    """Where an artifact of the recording lives, or None if it has none.

    Raw and summary go by the stored path alone (vts-lzad): a NULL path means
    "not produced (yet)" — a regeneration clears `summary_path` while the
    previous outputs/summary.md may still sit on disk, and serving it would
    show the result the user has just asked to replace. The HTTP routes answer
    "not ready" there, and every reader goes through this one rule.

    Only the redacted transcript falls back to its default name: legacy rows
    were never backfilled with a `redacted_path` but keep the file.
    """
    if kind == "raw":
        stored = getattr(recording, "transcript_path", None)
        return Path(stored) if stored else None
    if kind == "summary":
        stored = getattr(recording, "summary_path", None)
        return Path(stored) if stored else None
    stored = getattr(recording, "redacted_path", None)
    if stored:
        return Path(stored)
    root = Path(str(getattr(recording, "artifact_dir", "") or ""))
    return root / _DEFAULT_REDACTED_RELPATH


def read_recording_transcript(recording: Any, kind: ArtifactKind = "raw") -> str:
    """One text artifact of a recording.

    Raises RecordingArtifactMissing rather than returning "" — an empty string
    reads as "the recording says nothing", which is a different claim from "this
    artifact was never produced or has been archived away".
    """
    path = _artifact_path(recording, kind)
    if path is None:
        raise RecordingArtifactMissing(f"recording has no {kind} artifact (not ready)")
    if not path.exists():
        raise RecordingArtifactMissing(
            f"recording has no {kind} artifact ({path.name})"
        )
    return path.read_text(encoding="utf-8")


def recording_result_entries(recording: Any) -> list[dict[str, Any]]:
    """The recording's prompt results — the `prompt_results` column, dicts only.

    The legacy `meta["prompt_results"]` snapshot is deliberately not consulted:
    the column is kept current by the pipeline and is the only source.
    """
    return [
        e for e in (getattr(recording, "prompt_results", None) or [])
        if isinstance(e, dict)
    ]


def recording_result_path(recording: Any, source: str, ref: str) -> str | None:
    """The stored path of the result `(source, ref)`, or None.

    The system summary falls back to the recording's `summary_path` when no
    entry carries it.
    """
    for entry in recording_result_entries(recording):
        if str(entry.get("source")) == source and str(entry.get("id")) == ref:
            path = entry.get("path")
            if isinstance(path, str) and path:
                return path
    summary = getattr(recording, "summary_path", None)
    if source == "system" and ref == "summary" and summary:
        return summary
    return None


def _readable_result_path(recording: Any, source: str, ref: str) -> Path:
    """The file of the result `(source, ref)`, checked but not read.

    The path comes from the database, and a recording outlives the task that
    wrote it, so it is data of unclear age. Resolving it and requiring it to
    stay under the recording's artifact directory keeps a malformed or tampered
    row from turning a read into "open any file the server can reach".
    The system summary is also allowed at its own stored `summary_path`.

    Raises RecordingArtifactMissing when there is no entry, the path escapes
    the recording, or the file is gone.
    """
    raw_path = recording_result_path(recording, source, ref)
    if not raw_path:
        raise RecordingArtifactMissing(f"recording has no result {source}:{ref}")
    root_raw = str(getattr(recording, "artifact_dir", "") or "")
    try:
        path = Path(raw_path).resolve()
        allowed = bool(root_raw) and path.is_relative_to(Path(root_raw).resolve())
        summary = getattr(recording, "summary_path", None)
        if (
            not allowed and source == "system" and ref == "summary" and summary
            and path == Path(summary).resolve()
        ):
            allowed = True
        is_file = path.is_file()
    except (OSError, ValueError) as exc:
        raise RecordingArtifactMissing(f"result {source}:{ref} is not readable") from exc
    if not allowed:
        logger.warning(
            "prompt result path %s escapes recording dir %s; refusing",
            raw_path, root_raw,
        )
        raise RecordingArtifactMissing(f"result {source}:{ref} is outside the recording")
    if not is_file:
        raise RecordingArtifactMissing(f"result {source}:{ref} is not readable")
    return path


def read_recording_result(recording: Any, source: str, ref: str) -> str:
    """The text of one prompt result, read from the recording's own files.

    Raises RecordingArtifactMissing when there is no entry, the path escapes
    the recording, or the file is gone (see `_readable_result_path`).
    """
    path = _readable_result_path(recording, source, ref)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise RecordingArtifactMissing(f"result {source}:{ref} is not readable") from exc


def readable_result_entries(recording: Any) -> list[dict[str, Any]]:
    """The prompt results a reader can open: entries whose file is there.

    What a client is offered must be what `read_recording_result` serves — a
    pending entry (no file yet) or a result whose file vanished would only
    open as a 404 (vts-lzad).
    """
    out: list[dict[str, Any]] = []
    for entry in recording_result_entries(recording):
        try:
            _readable_result_path(
                recording, str(entry.get("source")), str(entry.get("id"))
            )
        except RecordingArtifactMissing:
            continue
        out.append(entry)
    return out


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

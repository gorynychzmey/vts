"""Keeping timecodes in the source media's timeline (vts-kb4z).

trim_initial_silence cuts leading silence off the WAV that ASR and diarization
read, so every time they report counts from where the speech starts. The
player, search hits and subtitles seek in the UNTRIMMED media. The cut length
was recorded in outputs/audio_preprocess.json and never read back, so on a
recording that opened with 9.3 s of silence every sentence played 9.3 s before
it was spoken. Measured on production: 55 of 123 recordings off by more than a
second, the worst by 12.5 s.

The working audio keeps its trimmed timeline: ASR chunks and preview clips are
cut from it, and those cuts must stay where they were. The offset is added at
the two points where times leave the pipeline — an ASR segment row and the
diarization artifact. Everything built from them (transcript.json, the player's
sentences, the search index) then lands on the source timeline by itself.

Artifacts written before the fix are moved by a data migration through
`migrate_old_timelines`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from vts.services.storage import write_json

log = logging.getLogger(__name__)

TRIM_MARKER = "audio_preprocess.json"

# Written by the migration into each file it moved, holding the seconds it
# added. A migration that dies half-way is run again, and without this a
# second pass would shift the already-moved files once more.
OFFSET_KEY = "source_timeline_offset_sec"


def source_offset(outputs_dir: Path) -> float:
    """Seconds trimmed off the start of the working audio, 0.0 when unknown.

    Unknown means the trim step never ran, or its marker cannot be read.
    Shifting by a guess would move times that are correct, so the answer is
    then "nothing was trimmed".
    """
    try:
        payload = json.loads((outputs_dir / TRIM_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    value = payload.get("trimmed_seconds") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def shift_spans(spans: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    """Copies of `spans` with `start` and `end` moved by `offset`.

    Copies, because callers still need the working-audio times: diarization
    cuts its preview clips from the trimmed WAV with the unshifted ones.
    """
    shifted = []
    for span in spans:
        moved = dict(span)
        for key in ("start", "end"):
            value = moved.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                moved[key] = float(value) + offset
        shifted.append(moved)
    return shifted


# Each artifact that carries absolute times, and the lists inside it that hold
# them. asr/segments_raw.json is listed by its CHUNKS only: the sentences
# inside a chunk are local to it, so moving the chunk moves them.
_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("outputs/transcript.json", ("entries", "all_entries")),
    ("asr/segments_raw.json", ("segments",)),
    ("outputs/diarization.json", ("segments",)),
)


def _migrate_file(path: Path, lists: tuple[str, ...], offset: float) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict) or OFFSET_KEY in payload:
        return
    for name in lists:
        items = payload.get(name)
        if isinstance(items, list):
            payload[name] = shift_spans([i for i in items if isinstance(i, dict)], offset)
    payload[OFFSET_KEY] = offset
    write_json(path, payload)


def migrate_artifact_dir(artifact_dir: Path) -> float:
    """Move one artifact directory's files onto the source timeline.

    Returns the offset applied, 0.0 when there was nothing to apply. Files
    already carrying OFFSET_KEY are left as they are, so this is safe to run
    again over a directory it already moved.
    """
    offset = source_offset(artifact_dir / "outputs")
    if offset <= 0:
        return 0.0
    for relative, lists in _ARTIFACTS:
        _migrate_file(artifact_dir / relative, lists, offset)
    return offset


def migrate_old_timelines(conn: Any) -> int:
    """Data migration: move every pre-fix recording onto the source timeline.

    `conn` is a synchronous SQLAlchemy connection (what alembic's op.get_bind()
    returns), inside the migration's transaction. Returns how many artifact
    directories were moved.

    The rows and the files cannot share one transaction, so each is made safe
    to repeat on its own terms. The rows move inside the migration's
    transaction: if it fails, none moved, and the rerun moves all of them. The
    files are rewritten one by one and each records what it carries
    (OFFSET_KEY), so the rerun skips those already moved.

    A directory whose trim marker cannot be read is skipped together with its
    rows: a deployment whose migration runs without the data volume keeps
    consistent, if still shifted, data rather than rows that disagree with
    their files.
    """
    from sqlalchemy import text

    dirs = conn.execute(
        text(
            "SELECT artifact_dir FROM tasks UNION SELECT artifact_dir FROM recordings"
        )
    ).scalars().all()
    moved = 0
    for artifact_dir in dirs:
        if not artifact_dir:
            continue
        offset = migrate_artifact_dir(Path(artifact_dir))
        if offset <= 0:
            continue
        params = {"dir": artifact_dir, "offset": offset}
        conn.execute(
            text(
                "UPDATE asr_segments SET start_sec = start_sec + :offset, "
                "end_sec = end_sec + :offset "
                "WHERE task_id IN (SELECT id FROM tasks WHERE artifact_dir = :dir)"
            ),
            params,
        )
        conn.execute(
            text(
                "UPDATE transcript_chunks SET start_sec = start_sec + :offset, "
                "end_sec = end_sec + :offset "
                "WHERE recording_id IN (SELECT id FROM recordings WHERE artifact_dir = :dir)"
            ),
            params,
        )
        moved += 1
    log.info("source timeline: moved %d artifact directories", moved)
    return moved

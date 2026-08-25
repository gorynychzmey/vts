from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from vts.db.repo import Repo
from vts.services.diarization.merge import (
    label_map,
    render_cleaned_transcript,
    speaker_label_word,
)
from vts.services.storage import write_json_atomic

_log = logging.getLogger(__name__)


def resolve_noise_labels(
    matches: dict[str, Any], decision_noise: set[str], has_decisions: bool
) -> set[str]:
    """Which labels are noise: the operator's decisions when any exist,
    otherwise the auto-suggestion stored in speaker_matches.json."""
    if has_decisions:
        return set(decision_noise)
    return {label for label, m in matches.items() if isinstance(m, dict) and m.get("noise")}


async def rerender_transcript(task, session, *, language: str | None) -> None:
    """Re-render transcript.json/.txt, excluding noise labels and substituting
    registry names.

    Rebuilds from `all_entries` — the unfiltered set written once at merge —
    never from `entries`, which is this function's own previous output. That
    distinction is the difference between reversible and destructive: filtering
    the output and writing it back narrowed the set on every pass, so a single
    run with the wrong labels deleted the excluded speakers for good and
    unticking the box afterwards restored nothing (vts-ra24).

    Genuinely idempotent, and genuinely undoable: the same marks give the same
    result, and removing a mark brings that speaker's lines back.
    """
    outputs = Path(task.artifact_dir) / "outputs"
    transcript_json = outputs / "transcript.json"
    if not transcript_json.exists():
        return
    try:
        payload = json.loads(transcript_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    # `all_entries` is the source of truth. Transcripts written before it
    # existed only carry `entries`; fall back to those so an older task still
    # re-renders — it cannot recover what an earlier pass already dropped, but
    # it stops losing more.
    entries = payload.get("all_entries")
    if not isinstance(entries, list):
        entries = payload.get("entries")
    if not isinstance(entries, list):
        return

    repo = Repo(session)
    names = await repo.speaker_names_for_task(task.user_id, task.id)
    decision_noise = await repo.noise_labels_from_decisions(task.user_id, task.id)
    # "Any decision saved" is NOT the same as "any noise decision": an operator
    # who resolved the task and marked nobody as noise (or unchecked an auto
    # suggestion) leaves decision_noise empty but has_decisions True. That
    # explicit all-clear must win over the stale auto-suggestion (vts-552).
    has_decisions = await repo.has_decisions_for_task(task.user_id, task.id)

    matches: dict[str, Any] = {}
    matches_path = outputs / "speaker_matches.json"
    if matches_path.exists():
        try:
            loaded = json.loads(matches_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                matches = loaded
        except (OSError, json.JSONDecodeError):
            matches = {}

    noise = resolve_noise_labels(matches, decision_noise, has_decisions=has_decisions)

    kept = [e for e in entries if str(e.get("speaker")) not in noise]
    if not kept:
        _log.warning(
            "rerender_transcript: all speakers flagged noise for task %s; "
            "rendering all rather than an empty transcript",
            task.id,
        )
        kept = list(entries)

    mapping = label_map(kept, speaker_label_word(language), names=names)
    text = render_cleaned_transcript(kept, mapping)

    new_payload = dict(payload)
    new_payload["entries"] = kept
    # Carry the unfiltered set forward, so the next pass has the same starting
    # point rather than whatever this one happened to keep.
    new_payload["all_entries"] = entries
    new_payload["text"] = text
    write_json_atomic(transcript_json, new_payload)
    (outputs / "transcript.txt").write_text(text, encoding="utf-8")

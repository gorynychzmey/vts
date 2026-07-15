"""Pure merge helpers: diarization segments -> transcript entries.

No I/O, no HTTP — everything here takes plain data and returns plain data so the
merge rules stay testable without a diarization backend.
"""

from __future__ import annotations

from typing import Any

DiarSegment = dict[str, Any]


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def speaker_at(
    diar_segments: list[DiarSegment],
    start: float,
    end: float,
) -> str | None:
    """Speaker whose diarization segment overlaps [start, end] the most.

    Ties resolve to the earliest segment, so the result never depends on sort
    stability of the caller's input.
    """
    best_speaker: str | None = None
    best_overlap = 0.0
    for segment in diar_segments:
        overlap = _overlap(start, end, float(segment["start"]), float(segment["end"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = str(segment["speaker"])
    return best_speaker


# whisper.cpp emits subword tokens in `words` ("к", "от", "ор", "ые"), which are
# useless for splitting utterances. Real words are longer and mostly not glued
# fragments; a corpus where most tokens are 1-2 chars is a tokenizer artifact,
# not speech.
_SUBWORD_MAX_LEN = 2
_SUBWORD_RATIO = 0.5


def usable_words(raw_json: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Word-level timestamps from a Whisper payload, or None when unusable.

    Returns None when the backend gave no words, gave words without timestamps,
    or gave subword fragments (whisper.cpp). Callers fall back to whole-entry
    attribution in that case.
    """
    segments = raw_json.get("segments")
    if not isinstance(segments, list):
        return None

    words: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            if word.get("start") is None or word.get("end") is None:
                return None
            words.append(word)

    if not words:
        return None

    short = sum(1 for w in words if len(str(w.get("word", "")).strip()) <= _SUBWORD_MAX_LEN)
    if short / len(words) > _SUBWORD_RATIO:
        return None
    return words


def _group_words_by_speaker(
    words: list[dict[str, Any]],
    diar_segments: list[DiarSegment],
) -> list[dict[str, Any]]:
    """Consecutive words sharing a speaker, collapsed into groups.

    Words that overlap no diarization segment inherit the running speaker, so a
    gap in diarization never drops text.
    """
    groups: list[dict[str, Any]] = []
    for word in words:
        start = float(word["start"])
        end = float(word["end"])
        speaker = speaker_at(diar_segments, start, end)
        if groups and (speaker is None or groups[-1]["speaker"] == speaker):
            groups[-1]["words"].append(word)
            groups[-1]["end"] = end
            continue
        groups.append({"speaker": speaker, "words": [word], "start": start, "end": end})
    return groups


def _absorb_small_groups(
    groups: list[dict[str, Any]],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Fold groups below the thresholds into their neighbour.

    Short backchannels ("угу") are exactly where diarization is least reliable,
    so splitting on them trades readable text for a low-confidence signal. They
    merge into the PREVIOUS group, which keeps the text in speaking order; a
    leading fragment has no previous group and folds forward instead.
    """
    kept: list[dict[str, Any]] = []
    leading_fragment: dict[str, Any] | None = None

    for i, group in enumerate(groups):
        big_enough = (
            len(group["words"]) >= min_words
            and (group["end"] - group["start"]) >= min_seconds
        )
        if big_enough or group["speaker"] is None:
            if group["speaker"] is None and kept:
                kept[-1]["words"].extend(group["words"])
                kept[-1]["end"] = group["end"]
                continue
            # This is a big group or None-speaker group
            if leading_fragment is not None:
                # Absorb any leading fragment into this group
                group["words"] = leading_fragment["words"] + group["words"]
                group["start"] = leading_fragment["start"]
                leading_fragment = None
            kept.append(group)
            continue
        # Below threshold: absorb into previous if exists, else save for forward merge
        if kept:
            kept[-1]["words"].extend(group["words"])
            kept[-1]["end"] = group["end"]
        else:
            # Leading small fragment: save it to absorb into next big group
            if leading_fragment is None:
                leading_fragment = {
                    "speaker": group["speaker"],
                    "words": group["words"][:],
                    "start": group["start"],
                    "end": group["end"],
                }
            else:
                # Multiple leading small groups: accumulate
                leading_fragment["words"].extend(group["words"])
                leading_fragment["end"] = group["end"]

    return _merge_adjacent_same_speaker(kept)


def _merge_adjacent_same_speaker(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        if merged and merged[-1]["speaker"] == group["speaker"]:
            merged[-1]["words"].extend(group["words"])
            merged[-1]["end"] = group["end"]
            continue
        merged.append(group)
    return merged


def _group_text(group: dict[str, Any]) -> str:
    return " ".join(str(w.get("word", "")).strip() for w in group["words"] if str(w.get("word", "")).strip())


def split_entry_by_speaker(
    entry: dict[str, Any],
    words: list[dict[str, Any]],
    diar_segments: list[DiarSegment],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Split one transcript entry where the speaker genuinely changes.

    A group becomes its own entry only when it clears BOTH thresholds; smaller
    groups are absorbed. When nothing clears them, the entry stays whole and is
    attributed by maximum overlap.
    """
    if not words:
        speaker = speaker_at(diar_segments, float(entry["start"]), float(entry["end"]))
        return [{**entry, "speaker": speaker}]

    groups = _absorb_small_groups(
        _group_words_by_speaker(words, diar_segments),
        min_words,
        min_seconds,
    )
    if len(groups) <= 1:
        speaker = groups[0]["speaker"] if groups else None
        if speaker is None:
            speaker = speaker_at(diar_segments, float(entry["start"]), float(entry["end"]))
        return [{**entry, "speaker": speaker}]

    return [
        {
            "start": group["start"],
            "end": group["end"],
            "text": _group_text(group),
            "speaker": group["speaker"],
        }
        for group in groups
    ]


def merge_entries(
    entries: list[dict[str, Any]],
    raw_json_by_index: dict[int, dict[str, Any]],
    diar_segments: list[DiarSegment],
    min_words: int,
    min_seconds: float,
) -> list[dict[str, Any]]:
    """Attribute every transcript entry to a speaker.

    Two levels of precision, not two algorithms: entries whose chunk carried
    usable word timestamps get split on genuine turn changes; the rest fall back
    to whole-entry maximum overlap.

    `raw_json_by_index` maps an entry's source chunk index to that chunk's
    Whisper payload. Entries with no matching payload take the fallback path.
    """
    merged: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        raw = raw_json_by_index.get(index)
        words = usable_words(raw) if isinstance(raw, dict) else None
        entry_words = (
            [
                word
                for word in words
                if float(word["end"]) > float(entry["start"])
                and float(word["start"]) < float(entry["end"])
            ]
            if words
            else []
        )
        merged.extend(
            split_entry_by_speaker(entry, entry_words, diar_segments, min_words, min_seconds)
        )
    return merged

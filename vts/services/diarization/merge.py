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

"""Rendering a transcript as subtitles (vts-fkyq / VOS-128).

The raw transcript can be read as running text or as SUBTITLES. WebVTT is the
format: unlike SRT it carries the speaker natively as a voice tag
(`<v Name>text`), which players understand, so speakers survive the export
instead of being glued into the sentence.

Input is the same block structure the /player page already builds
(`build_player_blocks`): blocks carry a resolved display `label` — the registry
name or "Voice N", never the raw SPEAKER_NN tag — and the sentences inside them
carry their own absolute timings. One source of truth feeds both views, so a
speaker renamed in the registry re-renders correctly here too.

Subtitles must work WITHOUT speakers. Diarization is optional and most
transcripts have `speaker=None`; such a block has an empty label and simply
gets no voice tag. That is the common case, not a degraded one.

This module is pure: no DB, no filesystem.
"""
from __future__ import annotations

from typing import Any

_HEADER = "WEBVTT"


def format_timestamp(seconds: float) -> str:
    """`HH:MM:SS.mmm`, the only timestamp shape a WebVTT parser accepts.

    Hours are always written, so a two-hour recording does not wrap around.
    Negative input is clamped to zero: a malformed timing should cost one
    misplaced cue, not a track the parser rejects outright.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0
    if total < 0 or total != total:  # NaN compares false against itself
        total = 0.0
    millis = int(round(total * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _cue_text(text: str) -> str:
    """Collapse a cue payload into lines that cannot terminate the cue early.

    A blank line ends a cue in WebVTT, so text carrying one would silently
    truncate the track. A line consisting of `-->` would likewise be read as the
    next cue's timing. Both are neutralised here rather than trusted upstream.
    """
    lines = [line.strip() for line in str(text).splitlines()]
    lines = [line for line in lines if line and line != "-->"]
    return " ".join(lines).strip()


def render_webvtt(blocks: list[dict[str, Any]]) -> str:
    """A WebVTT track built from player blocks.

    Every sentence becomes its own cue, so a subtitle line matches a sentence
    rather than the whole multi-minute block. The voice tag is repeated on each
    cue of a diarized block: a cue stands alone on screen, and a viewer who sees
    only the second one must still know who is speaking.

    With no blocks the result is a header-only track — valid WebVTT, unlike an
    empty string — so a transcript that is not ready yet still parses.
    """
    parts: list[str] = [_HEADER]
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        label = str(block.get("label") or "").strip()
        for sentence in block.get("sentences") or []:
            if not isinstance(sentence, dict):
                continue
            text = _cue_text(sentence.get("text") or "")
            if not text:
                continue
            try:
                start = float(sentence.get("start"))
                end = float(sentence.get("end"))
            except (TypeError, ValueError):
                continue
            payload = f"<v {label}>{text}" if label else text
            parts.append(
                f"{format_timestamp(start)} --> {format_timestamp(end)}\n{payload}"
            )
    return "\n\n".join(parts) + "\n"

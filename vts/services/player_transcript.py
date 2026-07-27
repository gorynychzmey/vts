"""Two-level transcript for the /player page (vts-u6w).

The player keeps the transcript's existing BLOCK structure — an ASR segment for
undiarized audio, a speaker turn for diarized — but exposes the SENTENCES inside
each block as individually clickable units, so a click seeks to that sentence
rather than to the start of a ~5-minute chunk.

Blocks are the transcript.json entries (already speaker-attributed). Sentences
are the inner ASR segments carried in asr/segments_raw.json, whose timings are
chunk-local and get shifted into absolute time here. This module is pure (no DB,
no filesystem): the API layer loads the two artifacts and the registry names,
then calls build_player_blocks.
"""
from __future__ import annotations

from typing import Any

from vts.services.diarization.merge import label_map


def _shifted_inner_sentences(raw_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten every chunk's inner ASR segments into absolute-time sentences.

    Each raw segment is one transcribed chunk with a `start` offset (absolute)
    and a `raw_json.segments` list whose `start`/`end` are LOCAL to the chunk
    (0-based). Shift them onto the chunk offset so sentences, blocks and the
    media clock all share one frame.
    """
    sentences: list[dict[str, Any]] = []
    for chunk in raw_segments:
        if not isinstance(chunk, dict):
            continue
        try:
            offset = float(chunk.get("start"))
        except (TypeError, ValueError):
            continue
        raw_json = chunk.get("raw_json")
        inner = raw_json.get("segments") if isinstance(raw_json, dict) else None
        if not isinstance(inner, list):
            continue
        for seg in inner:
            if not isinstance(seg, dict):
                continue
            try:
                s = float(seg.get("start")) + offset
                e = float(seg.get("end")) + offset
            except (TypeError, ValueError):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            sentences.append({"start": s, "end": e, "text": text})
    sentences.sort(key=lambda x: x["start"])
    return sentences


def build_player_blocks(
    entries: list[dict[str, Any]],
    raw_segments: list[dict[str, Any]],
    *,
    names: dict[str, str] | None = None,
    label_word: str = "Speaker",
) -> list[dict[str, Any]]:
    """Blocks with clickable sentences for the /player page.

    Returns one dict per block:
      {start, end, text, speaker, label, sentences: [{start, end, text}]}

    * `label` is the display label for a diarized block — the registry name or
      "<label_word> N" from label_map, never the raw SPEAKER_NN tag — and is
      empty for an undiarized (no-speaker) transcript.
    * `sentences` are the inner ASR sentences whose midpoint falls inside the
      block's [start, end). A block with no matching inner sentences (e.g. a
      backend that produced no per-sentence timings) degrades to a single
      clickable sentence at the block's own start — never fabricated sub-times.
    """
    all_sentences = _shifted_inner_sentences(raw_segments)
    mapping = label_map(entries, label_word, names=names or {})

    blocks: list[dict[str, Any]] = []
    for entry in entries:
        try:
            b_start = float(entry.get("start"))
            b_end = float(entry.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(entry.get("text") or "").strip()
        speaker = entry.get("speaker")

        # Sentences whose midpoint lands in this block's window. Midpoint (not
        # overlap) keeps each sentence in exactly one block even when adjacent
        # blocks share an edge.
        matched = [
            s for s in all_sentences
            if b_start <= (s["start"] + s["end"]) / 2.0 < b_end
        ]
        if matched:
            sentences = [
                {"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in matched
            ]
        else:
            # Fallback: the whole block is one clickable unit at its own start.
            sentences = [{"start": b_start, "end": b_end, "text": text}]

        blocks.append({
            "start": b_start,
            "end": b_end,
            "text": text,
            "speaker": speaker,
            "label": mapping.get(speaker, "") if speaker is not None else "",
            "sentences": sentences,
        })
    return blocks

"""Splitting a transcript into semantic chunks (vts-twe7 / VOS-131).

The requirement is to chunk on TIMESTAMPS and SPEAKER BOUNDARIES rather than on
a fixed token count — and the measured input shows why neither extreme works.
On a real task (39350783, 91 entries): the median entry is 169 characters, but
31 of 91 fall under 100 and 5 run past 1000. Speaker turns are therefore the
right SEAMS, not the right SIZES: short turns have to be merged and long ones
split.

The two directions are not symmetric:

* **splitting is safe** — a long turn belongs to one speaker, so no boundary is
  crossed and the timings can be interpolated inside the turn's own range;
* **merging crosses a seam**, so a merged chunk records every speaker it spans.
  A dialogue of one-liners still has to become a retrievable chunk, but a
  citation that attributed the whole passage to whoever spoke first would be
  wrong, and the speaker list is what stops that.

Chunking never depends on diarization being present: most transcripts have
`speaker=None`, and those chunk on size and time alone.

Sizes are in CHARACTERS, not tokens. A tokenizer would tie chunking to one
model's vocabulary, and the boundaries here come from speech structure anyway;
characters keep the module free of that dependency. The defaults bracket the
measured median rather than being round numbers picked for their looks.
"""
from __future__ import annotations

import re
from typing import Any

# Below this a chunk carries too little to retrieve on its own; above it, the
# passage covers several thoughts and the embedding blurs across them.
MIN_CHARS = 100
MAX_CHARS = 1000
# What a chunk aims for, as opposed to what it is allowed to be. Closing a chunk
# the moment it cleared MIN_CHARS produced uniformly minimal chunks and stranded
# the following short turn on its own; aiming higher lets neighbouring turns
# accumulate into something worth retrieving. Roughly three median turns (the
# measured median is 169 characters).
TARGET_CHARS = 500

# Sentence enders, kept with the sentence they end.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_entries(entries: Any) -> list[dict[str, Any]]:
    """Entries with usable text and timings, in time order."""
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start, end = _as_float(entry.get("start")), _as_float(entry.get("end"))
        # Missing timings are skipped rather than guessed: a chunk exists to be
        # cited, and a fabricated timecode would point the reader at the wrong
        # moment in the audio.
        if start is None or end is None:
            continue
        # Whisper wraps its output, so an entry is full of line breaks that say
        # nothing about the speech. Measured on the real corpus: 99% of chunks
        # carried them through. A chunk is what a search result quotes, so its
        # text has to read as prose rather than as a ragged column.
        text = " ".join(str(entry.get("text") or "").split())
        if not text:
            continue
        speaker = entry.get("speaker")
        out.append({
            "start": start,
            "end": max(end, start),
            "text": text,
            "speaker": str(speaker) if speaker else None,
        })
    out.sort(key=lambda e: e["start"])
    return out


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """A long passage cut on sentence ends, never mid-word.

    Falls back to a hard cut only for a single sentence longer than the cap —
    rare, and a chunk that exceeds the cap would blur its own embedding.
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        while len(sentence) > max_chars:
            # No sentence boundary to use: cut at the last space inside the cap
            # so a word is not broken in half.
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        current = sentence
    if current:
        pieces.append(current)
    # A last piece below the floor is a stub: real speech often ends a long turn
    # on a clause with no sentence end, which left 16-45 character chunks in the
    # MIDDLE of recordings (measured on task 39350783). Merging it back usually
    # overflows the cap — 979 + 90 against a 1000 limit in the measured case —
    # so REBALANCE instead: hand sentences from the previous piece to the stub
    # until it clears the floor. Both pieces stay within the cap, and neither is
    # a fragment.
    if len(pieces) > 1 and len(pieces[-1]) < MIN_CHARS:
        stub = pieces.pop()
        previous = pieces.pop()
        merged = f"{previous} {stub}".strip()
        if len(merged) <= max_chars:
            pieces.append(merged)
        else:
            donors = [x for x in _SENTENCE_SPLIT.split(previous) if x.strip()]
            moved: list[str] = []
            while donors and len(" ".join(moved + [stub])) < MIN_CHARS:
                moved.insert(0, donors.pop())
            head = " ".join(donors).strip()
            tail = " ".join(moved + [stub]).strip()
            # Only accept the rebalance if it actually fixed things; otherwise
            # keep the original split rather than trading one bad shape for
            # another.
            if head and len(head) >= MIN_CHARS and len(tail) <= max_chars:
                pieces.extend([head, tail])
            else:
                pieces.extend([previous, stub])
    return pieces or [text[:max_chars]]


def _emit_split(entry: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    """One over-long turn as several chunks, timings interpolated inside it.

    Interpolated by character share rather than evenly: a longer piece took
    longer to say. The first chunk keeps the turn's real start and the last its
    real end, so the split never drifts outside the turn it came from.
    """
    pieces = _split_long_text(entry["text"], max_chars)
    if len(pieces) == 1:
        return [{
            "start": entry["start"], "end": entry["end"],
            "text": pieces[0], "speakers": _speakers([entry]),
        }]
    total = sum(len(p) for p in pieces) or 1
    span = entry["end"] - entry["start"]
    out: list[dict[str, Any]] = []
    consumed = 0
    for index, piece in enumerate(pieces):
        start = entry["start"] + span * (consumed / total)
        consumed += len(piece)
        end = entry["start"] + span * (consumed / total)
        if index == 0:
            start = entry["start"]
        if index == len(pieces) - 1:
            end = entry["end"]
        out.append({
            "start": start, "end": max(end, start),
            "text": piece, "speakers": _speakers([entry]),
        })
    return out


def _speakers(entries: list[dict[str, Any]]) -> list[str]:
    """Every speaker a chunk spans, in the order they first spoke."""
    seen: list[str] = []
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker and speaker not in seen:
            seen.append(speaker)
    return seen


def chunk_entries(
    entries: Any,
    *,
    min_chars: int = MIN_CHARS,
    max_chars: int = MAX_CHARS,
    target_chars: int = TARGET_CHARS,
) -> list[dict[str, Any]]:
    """Transcript entries as retrievable chunks.

    Each chunk is `{index, start, end, text, speakers}`. `speakers` holds the
    technical SPEAKER_NN tags, not display names: names are substituted at
    render time, so a rename does not invalidate the index (the same property
    the player relies on).
    """
    cleaned = _clean_entries(entries)
    if not cleaned:
        return []

    chunks: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        text = " ".join(e["text"] for e in pending).strip()
        chunks.append({
            "start": pending[0]["start"],
            "end": max(e["end"] for e in pending),
            "text": text,
            "speakers": _speakers(pending),
        })
        pending.clear()

    for entry in cleaned:
        if len(entry["text"]) > max_chars:
            # A long turn stands on its own, so whatever was accumulating has to
            # come out first to keep time order. But flushing unconditionally
            # emitted the pending remainder as a chunk of its own even when it
            # was a single "Да." — the exact sub-minimum fragment this module
            # exists to avoid. Carry a short remainder into the split instead:
            # it belongs with what follows, and the first piece grows by a few
            # words rather than a useless chunk being born.
            pieces = _emit_split(entry, max_chars)
            pending_len = sum(len(e["text"]) + 1 for e in pending)
            if pending and pending_len < min_chars and pieces:
                prefix = " ".join(e["text"] for e in pending).strip()
                merged = f"{prefix} {pieces[0]['text']}".strip()
                if len(merged) <= max_chars:
                    pieces[0] = {
                        **pieces[0],
                        "start": pending[0]["start"],
                        "text": merged,
                        # The carried remainder may be another voice; a chunk
                        # spanning two speakers must say so.
                        "speakers": _speakers(pending + [entry]),
                    }
                    pending.clear()
            flush()
            chunks.extend(pieces)
            continue

        prospective = sum(len(e["text"]) + 1 for e in pending) + len(entry["text"])
        if pending and prospective > max_chars:
            flush()
        pending.append(entry)
        # Close on the TARGET size, not on the minimum. Closing at min_chars
        # made every chunk as small as it was allowed to be, and left the next
        # short turn standing alone — which is how a bare "Коротко." became a
        # chunk of its own even with a long turn right after it. The minimum is
        # a floor for what may be emitted, not the size to aim for.
        if sum(len(e["text"]) + 1 for e in pending) >= target_chars:
            flush()

    flush()

    # A short entry sandwiched between two chunks that had each just closed
    # ends up alone: it is not a tail, so the rule below never sees it, and it
    # cleared no threshold of its own. 24 of 6633 chunks over the whole real
    # corpus looked like this — "кипя и там же ключевое о чем говорится". Fold
    # each into its predecessor where the cap allows.
    def _absorb(host: dict[str, Any], guest: dict[str, Any], *, guest_first: bool) -> None:
        host["text"] = (
            f"{guest['text']} {host['text']}" if guest_first
            else f"{host['text']} {guest['text']}"
        ).strip()
        host["start"] = min(host["start"], guest["start"])
        host["end"] = max(host["end"], guest["end"])
        for speaker in guest["speakers"]:
            if speaker not in host["speakers"]:
                host["speakers"].append(speaker)

    compacted: list[dict[str, Any]] = []
    for position, chunk in enumerate(chunks):
        if len(chunk["text"]) >= min_chars:
            compacted.append(chunk)
            continue
        previous = compacted[-1] if compacted else None
        if previous and len(previous["text"]) + len(chunk["text"]) + 1 <= max_chars:
            _absorb(previous, chunk, guest_first=False)
            continue
        # Measured on the whole real corpus: for 20 of the 21 survivors the
        # PREVIOUS chunk was already 984-998 characters, so there was no room
        # left in it. Prepending to the NEXT one is the remaining home; only a
        # fragment with neither neighbour stays as it is.
        nxt = chunks[position + 1] if position + 1 < len(chunks) else None
        if nxt and len(nxt["text"]) + len(chunk["text"]) + 1 <= max_chars:
            _absorb(nxt, chunk, guest_first=True)
            continue
        compacted.append(chunk)
    chunks = compacted

    # A tail below the floor retrieves nothing on its own and only dilutes the
    # corpus — the real corpus produced a 17-character chunk this way. It goes
    # onto its predecessor, unless there is none: a recording whose whole
    # transcript is one short line must still be findable.
    if len(chunks) > 1 and len(chunks[-1]["text"]) < min_chars:
        tail = chunks.pop()
        previous = chunks[-1]
        merged = f"{previous['text']} {tail['text']}".strip()
        # Never past the cap: an over-long chunk blurs its own embedding, which
        # is the problem the cap exists to prevent.
        if len(merged) <= max_chars:
            previous["text"] = merged
            previous["end"] = max(previous["end"], tail["end"])
            for speaker in tail["speakers"]:
                if speaker not in previous["speakers"]:
                    previous["speakers"].append(speaker)
        else:
            chunks.append(tail)

    for index, chunk in enumerate(chunks):
        chunk["index"] = index
    return chunks

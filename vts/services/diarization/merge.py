"""Pure merge helpers: diarization segments -> transcript entries.

No I/O, no HTTP — everything here takes plain data and returns plain data so the
merge rules stay testable without a diarization backend.
"""

from __future__ import annotations

import re
from typing import Any

DiarSegment = dict[str, Any]


def _normalize_token(value: str) -> str:
    # Mirrors vts.pipeline.steps.transcription.normalize_token. Duplicated (not
    # imported) to avoid a cycle: transcription.py imports this module, so this
    # module cannot import back from transcription.py.
    return re.sub(r"[^\wа-яА-ЯёЁ]+", "", value, flags=re.UNICODE).strip().lower()


def trim_repetitive_units(units: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Drop leading/trailing runs of repeated units (Whisper hallucinations).

    Shared core of the repeat-detection heuristic: a unit here is whatever the
    caller considers atomic — a sentence for flat-text cleanup
    (`trim_repetitive_edges` in vts.pipeline.steps.transcription), or a whole
    transcript entry for the diarized path (`trim_repetitive_entries` below).
    Keeping this in one place means both paths agree on what counts as a
    hallucinated repeat instead of drifting apart.
    """
    removed_head = 0
    removed_tail = 0
    head_phrase: str | None = None
    tail_phrase: str | None = None
    min_repeats = 6

    remaining = list(units)

    while len(remaining) >= min_repeats:
        head = _normalize_token(remaining[0])
        if not head or len(head) > 64:
            break
        repeats = 0
        for unit in remaining:
            if _normalize_token(unit) == head:
                repeats += 1
            else:
                break
        if repeats < min_repeats:
            break
        removed_head += repeats
        head_phrase = remaining[0]
        remaining = remaining[repeats:]

    while len(remaining) >= min_repeats:
        tail = _normalize_token(remaining[-1])
        if not tail or len(tail) > 64:
            break
        repeats = 0
        for unit in reversed(remaining):
            if _normalize_token(unit) == tail:
                repeats += 1
            else:
                break
        if repeats < min_repeats:
            break
        removed_tail += repeats
        tail_phrase = remaining[-1]
        remaining = remaining[: len(remaining) - repeats]

    return remaining, {
        "removed_head_sentences": removed_head,
        "removed_tail_sentences": removed_tail,
        "head_phrase": head_phrase,
        "tail_phrase": tail_phrase,
    }


# Sentence boundaries: [.!?…] followed by whitespace. Imported by
# trim_repetitive_edges (vts.pipeline.steps.transcription) rather than
# re-derived there, so the flat and diarized paths cannot drift on what counts
# as a sentence — they must agree, or enabling diarization would silently
# change which text survives cleanup.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def trim_repetitive_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop leading/trailing SENTENCES that are a repeated ASR hallucination.

    Entry-list counterpart of `trim_repetitive_edges`: it must run BEFORE
    rendering so labels like "Голос 1:" (added only at render time) never enter
    the repeat-detection heuristic, which splits on `[.!?…]` and would corrupt
    or eat those labels if applied to already-rendered dialogue text.

    An entry is a multi-minute ASR chunk (`segment_target_seconds`), not a
    single sentence, so treating a whole entry as one atomic unit (as an
    earlier version of this function did) misses hallucinations that recur
    WITHIN an entry or span only a couple of entries — well under
    `min_repeats`. This flattens every entry's text into its component
    sentences, runs the exact same `trim_repetitive_units` heuristic
    `trim_repetitive_edges` uses over that flattened stream (so a repeat run
    crossing an entry boundary is still caught), then rebuilds entries from
    the surviving sentences. Each surviving sentence keeps its owning entry's
    speaker/start/end; an entry left with no surviving sentences is dropped.
    """
    empty_meta = {
        "removed_head_sentences": 0,
        "removed_tail_sentences": 0,
        "head_phrase": None,
        "tail_phrase": None,
    }
    if not entries:
        return list(entries), dict(empty_meta)

    # Flatten to (sentence, owning-entry-index) pairs across ALL entries.
    sentences: list[str] = []
    owners: list[int] = []
    for index, entry in enumerate(entries):
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        for piece in SENTENCE_SPLIT_RE.split(text):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
                owners.append(index)

    if not sentences:
        return list(entries), dict(empty_meta)

    kept_sentences, meta = trim_repetitive_units(sentences)
    # trim_repetitive_units only ever removes a prefix run and/or a suffix
    # run, so the surviving slice is contiguous — the two counts fully
    # determine which (sentence, owner) pairs survived.
    head_count = meta["removed_head_sentences"]
    tail_count = meta["removed_tail_sentences"]
    if head_count or tail_count:
        end_index = len(sentences) - tail_count
        kept_owners = owners[head_count:end_index]
    else:
        kept_owners = owners

    if not kept_sentences:
        # Same all-repeats safety net trim_repetitive_edges applies: trimming
        # every sentence away would leave nothing to show, which is worse
        # than showing the (hallucinated) original, so fall back to the
        # untrimmed entries. `meta` still reports what WOULD have been
        # removed, matching trim_repetitive_edges's contract.
        return list(entries), meta

    # Regroup surviving sentences by owning entry, preserving entry order and
    # each entry's own speaker/start/end. An entry that lost every one of its
    # sentences (fully inside the trimmed run) contributes nothing and is
    # dropped rather than kept with empty text.
    sentences_by_owner: dict[int, list[str]] = {}
    for owner, sentence in zip(kept_owners, kept_sentences):
        sentences_by_owner.setdefault(owner, []).append(sentence)

    kept_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        owned = sentences_by_owner.get(index)
        if not owned:
            continue
        # start/end stay entry-level, not text-level: when trimming consumes
        # most of a 300s chunk the span overstates the surviving text, and there
        # are no per-sentence timings at this layer to narrow it. Fine for the
        # transcript and for speaker lookup; a consumer seeking by timestamp
        # should not trust these to bound the text exactly.
        kept_entries.append({**entry, "text": " ".join(owned)})

    return kept_entries, meta


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

    Zero-length spans are matched by containment rather than area. Whisper emits
    words with start == end, and their overlap area is 0 against every segment —
    an area-only rule would leave them unattributed even in the middle of a
    confident segment.
    """
    if end <= start:
        for segment in diar_segments:
            if float(segment["start"]) <= start <= float(segment["end"]):
                return str(segment["speaker"])
        return None

    best_speaker: str | None = None
    best_overlap = 0.0
    for segment in diar_segments:
        overlap = _overlap(start, end, float(segment["start"]), float(segment["end"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = str(segment["speaker"])
    return best_speaker


def nearest_speaker(
    diar_segments: list[DiarSegment],
    start: float,
    end: float,
) -> str | None:
    """`speaker_at`, but fills gaps with the nearest segment instead of None.

    Whisper timestamps and pyannote boundaries come from independent models and
    do not line up to the word. At a speaker change, one or two words land in
    the silent gap between segments and overlap neither. Attributing them to the
    nearest segment edge puts them with the speaker they are acoustically
    closest to — usually the one about to talk — instead of stranding them on
    the previous speaker.

    This cannot be perfect: nobody spoke during the gap, so the true owner is
    unknowable. The ≥2-word / ≥0.8s split threshold absorbs the residual
    single-word misplacements downstream.
    """
    speaker = speaker_at(diar_segments, start, end)
    if speaker is not None:
        return speaker

    midpoint = (start + end) / 2
    nearest: str | None = None
    best_distance = float("inf")
    for segment in diar_segments:
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if seg_start <= midpoint <= seg_end:
            distance = 0.0
        else:
            distance = min(abs(midpoint - seg_start), abs(midpoint - seg_end))
        if distance < best_distance:
            best_distance = distance
            nearest = str(segment["speaker"])
    return nearest


def _glue_subwords(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join subword tokens into whole words.

    Whisper's tokenizer marks a word boundary with a LEADING SPACE, so a piece
    that continues the previous word arrives without one (" прог" + "он" +
    "яет" -> "прогоняет"). A glued word spans the first piece's start to the
    last piece's end — exactly the interval the speaker lookup needs.

    Verified against a real cpp task: 1321 tokens glued back into 694 words,
    matching Whisper's own text everywhere except one spot where Whisper itself
    broke a word across a newline ("инструмент\\nы.") and the gluing got it right.

    Gluing only runs when the payload actually carries that boundary marker. A
    backend that pre-strips its words gives no boundaries to read, and joining
    on "no leading space" would then weld every word into one — worse than the
    fallback it replaces.
    """
    if not any(str(word.get("word", "")).startswith(" ") for word in words):
        return [
            {**word, "word": str(word.get("word", "")).strip(), "start": float(word["start"]), "end": float(word["end"])}
            for word in words
            if str(word.get("word", "")).strip()
        ]

    glued: list[dict[str, Any]] = []
    for word in words:
        raw = str(word.get("word", ""))
        text = raw.strip()
        if not text:
            continue
        if raw.startswith(" ") or not glued:
            glued.append({**word, "word": text, "start": float(word["start"]), "end": float(word["end"])})
            continue
        glued[-1]["word"] += text
        glued[-1]["end"] = float(word["end"])
    return glued


def usable_words(raw_json: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Word-level timestamps from a Whisper payload, or None when unusable.

    Returns None only when the backend gave no words at all or gave them without
    timestamps. Subword tokens are glued rather than rejected: their timestamps
    are per-token, so word boundaries survive the join intact.
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

    glued = _glue_subwords(words)
    return glued or None


def _group_words_by_speaker(
    words: list[dict[str, Any]],
    diar_segments: list[DiarSegment],
) -> list[dict[str, Any]]:
    """Consecutive words sharing a speaker, collapsed into groups.

    A word in the silent gap between two speakers is assigned to the nearest
    segment (`nearest_speaker`), not left to inherit the running one — that is
    what moves a turn boundary onto the acoustic change instead of stranding the
    incoming speaker's first words on the previous group.
    """
    groups: list[dict[str, Any]] = []
    for word in words:
        start = float(word["start"])
        end = float(word["end"])
        speaker = nearest_speaker(diar_segments, start, end)
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


def drop_marginal_speakers(
    entries: list[dict[str, Any]],
    min_share: float,
) -> list[dict[str, Any]]:
    """Reassign speakers holding a negligible share of speech.

    Diarization invents phantom speakers on music, echo and noise. Such a
    phantom would flip a monologue into a two-voice dialogue, so anything below
    `min_share` of total speech time is folded into the dominant speaker.
    """
    totals: dict[str, float] = {}
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker is None:
            continue
        totals[speaker] = totals.get(speaker, 0.0) + (float(entry["end"]) - float(entry["start"]))

    if not totals:
        return list(entries)

    overall = sum(totals.values())
    if overall <= 0:
        return list(entries)

    dominant = max(totals, key=lambda key: totals[key])
    marginal = {speaker for speaker, total in totals.items() if (total / overall) < min_share}
    if not marginal:
        return list(entries)

    return [
        {**entry, "speaker": dominant} if entry.get("speaker") in marginal else dict(entry)
        for entry in entries
    ]


# Label word per recording language, so the rendered "Голос N:" / "Speaker N:"
# prefix matches the language the transcript is actually written in — the
# labels are read by both a human and (via segment_prompt.md's output-language
# instruction) an LLM that is told to rewrite anything not in that language.
# A Russian recording keeps "Голос" (the pre-existing, most common case);
# every other language — including ones without a dedicated word here — falls
# back to "Speaker", which reads as reasonable English regardless of the
# actual output language and is never mistaken for prose to "rewrite away".
_LABEL_WORDS: dict[str, str] = {
    "ru": "Голос",
    "en": "Speaker",
}
DEFAULT_LABEL_WORD = "Speaker"

# All label words `label_map` can ever produce, in a stable order. Consumers
# that must recognize a rendered label without knowing which language produced
# it (e.g. summarizer.split_utterances) key off this set instead of
# hardcoding "Голос", so they can never silently fall out of sync with this
# module when a new language is added here.
LABEL_WORDS: tuple[str, ...] = tuple(dict.fromkeys([*_LABEL_WORDS.values(), DEFAULT_LABEL_WORD]))


def speaker_label_word(language: str | None) -> str:
    """The label word ("Голос", "Speaker", ...) for a recording's language.

    Render-time only: never changes what is stored in entries[i]["speaker"]
    (the technical SPEAKER_00 tag), only what label_map prints for a reader.
    """
    key = (language or "").strip().lower()
    return _LABEL_WORDS.get(key, DEFAULT_LABEL_WORD)


def label_map(entries: list[dict[str, Any]], label_word: str = "Голос") -> dict[str, str]:
    """Technical tags -> "<label_word> N", numbered by first appearance.

    "<label_word> 1" is whoever spoke first, which is what a reader expects.
    The technical tag stays in the data; this mapping exists only for
    rendering. `label_word` defaults to "Голос" for callers that predate
    per-language labels (this module's own render_transcript wrapper, and
    tests exercising it directly) — new callers should pass
    speaker_label_word(language) explicitly.
    """
    mapping: dict[str, str] = {}
    for entry in entries:
        speaker = entry.get("speaker")
        if speaker is None or speaker in mapping:
            continue
        mapping[speaker] = f"{label_word} {len(mapping) + 1}"
    return mapping


def render_cleaned_transcript(cleaned: list[dict[str, Any]], mapping: dict[str, str]) -> str:
    """Render already-cleaned entries (post `drop_marginal_speakers`) to text.

    Split out from `render_transcript` so a caller that must also return the
    cleaned entries (e.g. `apply_diarization`) can run `drop_marginal_speakers`
    exactly once and derive both the entries it hands back AND the rendered
    text from that single cleaned list, instead of drifting by cleaning twice.
    """
    if len(mapping) <= 1:
        return " ".join(str(entry["text"]).strip() for entry in cleaned if str(entry["text"]).strip())

    # A sentinel distinct from any real speaker AND from the initial "no block yet"
    # state, so a leading None-speaker entry still opens its own bare block instead
    # of being mistaken for "nothing rendered yet".
    _NONE_SENTINEL = object()

    blocks: list[str] = []
    current: object | None = None
    for entry in cleaned:
        text = str(entry["text"]).strip()
        if not text:
            continue
        speaker = entry.get("speaker")
        # A None speaker means diarization covered nothing here — attributing it
        # to a neighbour would be a false claim about who said it, which is worse
        # than leaving it unlabelled. So it always starts its own block, never
        # merges into a labelled neighbour, and carries no "Голос N" prefix.
        key = _NONE_SENTINEL if speaker is None else speaker
        if key != current:
            blocks.append(text if speaker is None else f"{mapping[speaker]}: {text}")
            current = key
            continue
        blocks[-1] = blocks[-1] + " " + text
    return "\n\n".join(blocks)


def render_transcript(entries: list[dict[str, Any]], min_share: float, label_word: str = "Голос") -> str:
    """Flat text for a monologue, labelled turns for a dialogue.

    Thin wrapper kept for callers that only need text, not the cleaned entries
    (e.g. tests exercising this module directly). `apply_diarization` calls
    `drop_marginal_speakers` + `render_cleaned_transcript` itself instead, so
    the reassignment happens exactly once and its output is shared.
    `label_word` defaults to "Голос" so existing callers/tests keep their
    current (Russian) output unchanged.
    """
    cleaned = drop_marginal_speakers(entries, min_share)
    mapping = label_map(cleaned, label_word)
    return render_cleaned_transcript(cleaned, mapping)

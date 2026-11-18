"""Decomposing a raw ASR payload into storable axes (vts-6qwy).

`asr_segments.raw_json` holds whisper's entire answer. Most of that is either
duplicated (`segments[].text` and the top-level `text` are the same words again)
or internal to the model (`tokens` ids, `t_dtw`, `temperature`), and the payload
dominates the database.

The rule here is the owner's: do NOT delete raw data — bring it into a form that
is actually usable. Two orthogonal axes come out, and any view of the transcript
can be rebuilt from the pair:

  * `tokens`    — the word timeline, `[text, start, end, probability]`
  * `sentences` — the sentence timeline, `[start, end, text]`

Both are kept because the two real consumers read DIFFERENT granularities:
`merge_entries` walks `segments[].words[]` (token level) to attribute speakers,
while the /player page and the subtitles walk `segments[].start/end/text`
(sentence level). Dropping either breaks a shipped feature.

The third axis named in the task — the speaker-change timeline — is NOT built
here: it already exists separately as `outputs/diarization.json`.

**Arrays, not objects.** Measured at 54% of the size (~1.8x), and the whole
difference is not repeating the key names on every record. A machine reads this,
so the readability that would justify the keys buys nothing.

**Tokens are stored as the model emits them.** Whisper's word timings are
subword tokens (" прог" + "он" + "яет"); gluing them into whole words stays a
read-time step (`_glue_subwords`). That is the owner's decision and it follows
the same rule: gluing is processing, its algorithm may still change, and each
token carries its own `probability` that averaging would destroy.

`probability`, `avg_logprob` and `no_speech_prob` are kept deliberately. They
describe the quality of the SOURCE MATERIAL, not merely of the transcript, and
cannot be recovered later except by re-running ASR on audio that may be gone.
"""
from __future__ import annotations

import bisect
import copy
from typing import Any


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segments_of(raw_json: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_json, dict):
        return []
    segments = raw_json.get("segments")
    if not isinstance(segments, list):
        return []
    return [s for s in segments if isinstance(s, dict)]


def decompose_raw_json(raw_json: Any) -> dict[str, Any]:
    """Split a raw whisper payload into `{tokens, sentences, meta}`.

    Never raises: stored data can be malformed, and a bad row must degrade to
    empty axes rather than break the task reading it.
    """
    segments = _segments_of(raw_json)

    tokens: list[list[Any]] = []
    sentences: list[list[Any]] = []
    avg_logprob: list[float | None] = []
    no_speech_prob: list[float | None] = []

    for segment in segments:
        start = _as_float(segment.get("start"))
        end = _as_float(segment.get("end"))
        text = segment.get("text")
        # The metric axes are indexed BY SENTENCE on the way back, so they may
        # only grow when a sentence does. Appending unconditionally shifted
        # every metric after the first skipped segment onto the wrong sentence
        # (vts-belb) — and a wrong quality figure is worse than none, because it
        # still reads as a measurement.
        if start is not None and end is not None and isinstance(text, str):
            sentences.append([start, end, text])
            avg_logprob.append(_as_float(segment.get("avg_logprob")))
            no_speech_prob.append(_as_float(segment.get("no_speech_prob")))

        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            w_start = _as_float(word.get("start"))
            w_end = _as_float(word.get("end"))
            # A word without timings was never usable — usable_words() rejects
            # the whole payload over it — so carrying it would make the stored
            # axis claim more than the data supports.
            if w_start is None or w_end is None:
                continue
            word_text = word.get("word")
            if not isinstance(word_text, str):
                continue
            tokens.append([word_text, w_start, w_end, _as_float(word.get("probability"))])

    meta: dict[str, Any] = {}
    if isinstance(raw_json, dict):
        language = raw_json.get("language")
        if isinstance(language, str) and language:
            meta["language"] = language
        duration = _as_float(raw_json.get("duration"))
        if duration is not None:
            meta["duration"] = duration
    if avg_logprob:
        meta["avg_logprob"] = avg_logprob
    if no_speech_prob:
        meta["no_speech_prob"] = no_speech_prob

    return {"tokens": tokens, "sentences": sentences, "meta": meta}


def recompose_raw_json(decomposed: Any) -> dict[str, Any]:
    """Rebuild the payload shape the existing consumers read.

    This is what makes the decomposition safe to adopt without touching
    `usable_words` or `_shifted_inner_sentences`: they keep receiving the shape
    they already parse. Only the fields nothing reads are absent.

    Words are attached to the sentence they spell out, read in order: whisper
    emits a sentence's text as the concatenation of its tokens. Only when the
    texts do not line up does a word go to the sentence whose window contains
    its time. Order comes first because the times cannot be trusted for this:
    under --vad whisper.cpp reports words seconds before their sentence (see
    `realign_word_times`), and joining by time put them under an earlier one.
    Tokens outside every window still land in the payload — appended to the
    nearest preceding sentence — because losing a word here would silently
    change speaker attribution.
    """
    if not isinstance(decomposed, dict):
        return {"segments": []}

    raw_tokens = decomposed.get("tokens")
    tokens = [t for t in raw_tokens if isinstance(t, (list, tuple)) and len(t) >= 3] if isinstance(raw_tokens, list) else []
    raw_sentences = decomposed.get("sentences")
    sentences = [s for s in raw_sentences if isinstance(s, (list, tuple)) and len(s) >= 3] if isinstance(raw_sentences, list) else []
    meta = decomposed.get("meta") if isinstance(decomposed.get("meta"), dict) else {}

    avg_logprob = meta.get("avg_logprob") if isinstance(meta.get("avg_logprob"), list) else []
    no_speech_prob = meta.get("no_speech_prob") if isinstance(meta.get("no_speech_prob"), list) else []

    segments: list[dict[str, Any]] = []
    for index, sentence in enumerate(sentences):
        start, end, text = float(sentence[0]), float(sentence[1]), str(sentence[2])
        segment: dict[str, Any] = {"start": start, "end": end, "text": text, "words": []}
        if index < len(avg_logprob) and avg_logprob[index] is not None:
            segment["avg_logprob"] = avg_logprob[index]
        if index < len(no_speech_prob) and no_speech_prob[index] is not None:
            segment["no_speech_prob"] = no_speech_prob[index]
        segments.append(segment)

    words: list[dict[str, Any]] = []
    for token in tokens:
        text, t_start, t_end = str(token[0]), float(token[1]), float(token[2])
        probability = token[3] if len(token) > 3 else None
        word: dict[str, Any] = {"word": text, "start": t_start, "end": t_end}
        if probability is not None:
            word["probability"] = probability
        words.append(word)

    owners = _owners_by_text(segments, words)
    if owners is not None:
        for word, owner in zip(words, owners):
            segments[owner]["words"].append(word)
        words = []

    for word in words:
        t_start = word["start"]
        target = None
        for segment in segments:
            if segment["start"] <= t_start < segment["end"]:
                target = segment
                break
        if target is None:
            # Outside every window (a boundary token, or no sentences at all):
            # attach to the last sentence that starts before it, else the first.
            earlier = [s for s in segments if s["start"] <= t_start]
            target = earlier[-1] if earlier else (segments[0] if segments else None)
        if target is None:
            segments.append({"start": t_start, "end": t_end, "text": "", "words": [word]})
            continue
        target["words"].append(word)

    payload: dict[str, Any] = {"segments": segments}
    if meta.get("language"):
        payload["language"] = meta["language"]
    if meta.get("duration") is not None:
        payload["duration"] = meta["duration"]
    return payload


def segment_raw_payload(segment: Any) -> dict[str, Any]:
    """The payload shape consumers parse, from whichever form a row carries.

    One place decides this, rather than each consumer testing both columns:
    rows written before the decomposition still carry only `raw_json`, and rows
    written after carry both until the legacy column is cleared. Preferring
    `payload` means the cleanup does not have to be coordinated with a deploy.

    Word times are put back on the original timeline here, on read, so the
    stored answer stays exactly what whisper returned and rows written before
    the fix are corrected without a migration.
    """
    decomposed = getattr(segment, "payload", None)
    if isinstance(decomposed, dict) and (decomposed.get("tokens") or decomposed.get("sentences")):
        return realign_word_times(recompose_raw_json(decomposed))
    raw = getattr(segment, "raw_json", None)
    return realign_word_times(raw) if isinstance(raw, dict) else {}


def _letters(text: str) -> str:
    return "".join(text.split())


def _length(text: str) -> float:
    """Characters of `text`, ignoring whitespace.

    A multi-byte character can be cut between two tokens ("Й" arrives as two
    tokens of U+FFFD each), so a replacement character counts as half.
    """
    letters = _letters(text)
    halves = letters.count("\ufffd")
    return len(letters) - halves + halves / 2


def _owners_by_text(segments: list[dict[str, Any]], words: list[dict[str, Any]]) -> list[int] | None:
    """Index of the sentence each word belongs to, by walking the text.

    None when the words do not spell out the sentences, so the caller falls
    back to time and nothing is guessed. Lengths are compared to within half a
    character, which is what a cut multi-byte character can leave over.
    """
    if not segments or not words:
        return None
    owners: list[int] = []
    position = 0
    for index, segment in enumerate(segments):
        wanted = _length(segment["text"])
        got = 0.0
        while got < wanted - 0.25 and position < len(words):
            got += _length(words[position]["word"])
            owners.append(index)
            position += 1
        if abs(got - wanted) > 0.5:
            return None
    if position != len(words):
        return None
    return owners


# Silence removal only ever makes words EARLY: each cut pause pulls every
# later word back. A segment whose first word starts this much before the
# segment itself is the signature; correct timings stray by tenths at most.
_EARLY_SEC = 0.5
# One such segment can be a quirk of a single word; two make it systematic.
# Counting segments rather than words matters: a production chunk drifting by
# 1.6 s had only 8% of its words outside their own segment.
_EARLY_SEGMENTS = 2


def _early_segments(segments: list[dict[str, Any]]) -> int:
    count = 0
    for segment in segments:
        start = _as_float(segment.get("start"))
        words = segment.get("words") or []
        first = _as_float(words[0].get("start")) if words and isinstance(words[0], dict) else None
        if start is not None and first is not None and first < start - _EARLY_SEC:
            count += 1
    return count


def _anchors(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """(reported word time, original time) pairs, one at each segment edge."""
    points: list[tuple[float, float]] = []
    for segment in segments:
        start, end = _as_float(segment.get("start")), _as_float(segment.get("end"))
        timed = [
            w for w in segment.get("words") or []
            if _as_float(w.get("start")) is not None and _as_float(w.get("end")) is not None
        ]
        if start is None or end is None or not timed:
            continue
        points.append((float(timed[0]["start"]), start))
        points.append((float(timed[-1]["end"]), end))
    points.sort()
    # Silence removal never moves time backwards, so the map is monotonic;
    # a stray segment must not fold it.
    monotonic: list[tuple[float, float]] = []
    for reported, original in points:
        if monotonic and original < monotonic[-1][1]:
            original = monotonic[-1][1]
        monotonic.append((reported, original))
    return monotonic


def _interpolate(points: list[tuple[float, float]], t: float) -> float:
    reported = [p[0] for p in points]
    i = bisect.bisect_right(reported, t)
    if i == 0:
        return points[0][1] + (t - points[0][0])
    if i == len(points):
        return points[-1][1] + (t - points[-1][0])
    (r0, o0), (r1, o1) = points[i - 1], points[i]
    if r1 == r0:
        return o0
    return o0 + (t - r0) * (o1 - o0) / (r1 - r0)


def realign_word_times(raw_json: dict[str, Any]) -> dict[str, Any]:
    """Word times on the same timeline as their segments.

    whisper.cpp's server, run with --vad, maps each segment's start and end
    back onto the original audio but reports every word at its time in the
    audio left after silence removal (it emits `token.t0` unmapped). The gap
    grows with each pause; on production chunks it reached 20-40 s, and 100 s
    after a long non-speech intro. The server does not return the map, so it is
    rebuilt from the answer: every segment edge pairs a reported word time with
    the original time the server gave that segment, and word times are
    interpolated between those pairs. Checked against the server's own map
    (logged by the 2026-02 build) over 36k words: median error 0.01 s, p99
    0.8 s, against 6 s and 101 s before.

    Applied only when words systematically start before their own segment, so
    correct timings — another backend, or a whisper.cpp that maps word times
    itself — pass through unchanged, and so does an answer already realigned.
    Returns a copy; the argument is not modified.
    """
    segments = _segments_of(raw_json)
    if not segments or _early_segments(segments) < _EARLY_SEGMENTS:
        return raw_json
    points = _anchors(segments)
    if len(points) < 2:
        return raw_json
    result = copy.deepcopy(raw_json)
    for segment in _segments_of(result):
        for word in segment.get("words") or []:
            w_start, w_end = _as_float(word.get("start")), _as_float(word.get("end"))
            if w_start is None or w_end is None:
                continue
            word["start"] = _interpolate(points, w_start)
            word["end"] = _interpolate(points, w_end)
    return result

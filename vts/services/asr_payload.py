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

    Words are attached to the sentence whose window contains them, so a
    consumer walking `segments[].words[]` sees the same sequence as before.
    Tokens outside every sentence window still land in the payload — appended to
    the nearest preceding sentence — because losing a word here would silently
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

    for token in tokens:
        text, t_start, t_end = str(token[0]), float(token[1]), float(token[2])
        probability = token[3] if len(token) > 3 else None
        word: dict[str, Any] = {"word": text, "start": t_start, "end": t_end}
        if probability is not None:
            word["probability"] = probability
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
    """
    decomposed = getattr(segment, "payload", None)
    if isinstance(decomposed, dict) and (decomposed.get("tokens") or decomposed.get("sentences")):
        return recompose_raw_json(decomposed)
    raw = getattr(segment, "raw_json", None)
    return raw if isinstance(raw, dict) else {}

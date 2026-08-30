"""Decomposing a raw ASR payload into storable axes (vts-6qwy).

`asr_segments.raw_json` holds whisper's whole answer. Most of it is either
duplicated (segments[].text and the top-level text are the same words again) or
internal (model token ids, t_dtw, temperature), and the payload dominates the
database.

The rule this follows is the owner's: do NOT delete raw data, decompose it into
a form that is actually usable. Two axes come out:

  * `tokens`    — the word timeline: [text, start, end, probability]
  * `sentences` — the sentence timeline: [start, end, text]

Both are needed because the two real consumers read DIFFERENT granularities:
merge_entries walks segments[].words[] (token level) while the player and the
subtitles walk segments[].start/end/text (sentence level). Dropping either one
breaks a shipped feature.

Arrays rather than objects: measured at 54% of the size (~1.8x), entirely from
not repeating the key names on every record. A machine reads this, not a human.

Tokens are stored AS THE MODEL EMITS THEM — whisper's word timings are subword
tokens (" прог" + "он" + "яет"), and gluing them into whole words stays a
read-time step (owner's decision, 2026-08-29): gluing is processing whose
algorithm may still change, and each token carries its own probability that
averaging would destroy.
"""
from __future__ import annotations

from vts.services.asr_payload import decompose_raw_json, recompose_raw_json


def _payload(**over):
    base = {
        "text": " Привет мир",
        "language": "ru",
        "duration": 5.0,
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": 0.0,
                "end": 2.5,
                "text": " Привет",
                "tokens": [50364, 1234, 50464],
                "temperature": 0.0,
                "avg_logprob": -0.31,
                "no_speech_prob": 0.02,
                "words": [
                    {"word": " При", "start": 0.0, "end": 1.0, "probability": 0.9, "t_dtw": -1},
                    {"word": "вет", "start": 1.0, "end": 2.5, "probability": 0.8, "t_dtw": -1},
                ],
            },
            {
                "id": 1,
                "start": 2.5,
                "end": 5.0,
                "text": " мир",
                "tokens": [50464, 5678],
                "temperature": 0.0,
                "avg_logprob": -0.22,
                "no_speech_prob": 0.01,
                "words": [
                    {"word": " мир", "start": 2.5, "end": 5.0, "probability": 0.95},
                ],
            },
        ],
    }
    base.update(over)
    return base


# ----------------------------------------------------------------- word axis

def test_tokens_are_arrays_of_text_start_end_probability():
    out = decompose_raw_json(_payload())
    assert out["tokens"] == [
        [" При", 0.0, 1.0, 0.9],
        ["вет", 1.0, 2.5, 0.8],
        [" мир", 2.5, 5.0, 0.95],
    ]


def test_subword_tokens_are_stored_unglued():
    # " При" + "вет" stay two records: gluing is a read-time step, and each
    # token's own probability would be lost to averaging.
    out = decompose_raw_json(_payload())
    assert [t[0] for t in out["tokens"][:2]] == [" При", "вет"]


def test_leading_space_is_preserved_on_tokens():
    # The leading space IS the word boundary marker _glue_subwords reads.
    # Stripping it here would weld every word into one on the way back.
    out = decompose_raw_json(_payload())
    assert out["tokens"][0][0].startswith(" ")
    assert not out["tokens"][1][0].startswith(" ")


def test_missing_probability_is_kept_as_null_not_invented():
    payload = _payload()
    del payload["segments"][1]["words"][0]["probability"]
    out = decompose_raw_json(payload)
    assert out["tokens"][-1] == [" мир", 2.5, 5.0, None]


def test_word_without_timings_is_dropped_from_the_token_axis():
    # usable_words() returns None for such a payload, so these words were never
    # usable; carrying them would only make the stored axis lie.
    payload = _payload()
    payload["segments"][0]["words"][0]["start"] = None
    out = decompose_raw_json(payload)
    assert all(t[1] is not None and t[2] is not None for t in out["tokens"])


# ------------------------------------------------------------- sentence axis

def test_sentences_carry_start_end_text():
    out = decompose_raw_json(_payload())
    assert out["sentences"] == [
        [0.0, 2.5, " Привет"],
        [2.5, 5.0, " мир"],
    ]


def test_sentence_axis_survives_a_payload_with_no_words_at_all():
    # A backend that gives no word timings still gives sentences — the player
    # and the subtitles depend on exactly this level.
    payload = _payload()
    for seg in payload["segments"]:
        seg.pop("words")
    out = decompose_raw_json(payload)
    assert out["tokens"] == []
    assert len(out["sentences"]) == 2


# -------------------------------------------------------------------- meta

def test_meta_keeps_the_quality_metrics():
    # These describe the quality of the SOURCE MATERIAL, not just the
    # transcript, and cannot be recovered without re-running ASR.
    out = decompose_raw_json(_payload())
    assert out["meta"]["language"] == "ru"
    assert out["meta"]["duration"] == 5.0
    assert out["meta"]["avg_logprob"] == [-0.31, -0.22]
    assert out["meta"]["no_speech_prob"] == [0.02, 0.01]


def test_internal_fields_are_dropped():
    out = decompose_raw_json(_payload())
    blob = repr(out)
    # Model token ids, the always -1 t_dtw, the decoding temperature and the
    # per-segment id carry nothing a later consumer can use.
    assert "50364" not in blob
    assert "t_dtw" not in blob
    assert "temperature" not in blob


def test_duplicated_text_is_not_stored_twice():
    # The top-level text is the sentences joined; storing it again is pure
    # duplication.
    out = decompose_raw_json(_payload())
    assert "text" not in out
    assert "Привет мир" not in repr(out.get("meta", {}))


def test_quality_metrics_stay_with_their_own_sentence():
    """A skipped segment must not shift every later metric by one (vts-belb).

    `sentences` was appended CONDITIONALLY (a segment with no text is not a
    sentence) while the metric axes were appended UNCONDITIONALLY, and recompose
    indexed the metrics by sentence position. From the first skipped segment
    onward, every avg_logprob and no_speech_prob described a different sentence
    than the one it was attached to.

    These are exactly the values kept because they describe the quality of the
    SOURCE MATERIAL — a wrong one is worse than none, since it reads as a
    measurement.
    """
    raw = {"segments": [
        {"start": 0.0, "end": 1.0, "text": "A", "avg_logprob": -0.11, "no_speech_prob": 0.01},
        # No text: not a sentence, so it contributes nothing to either axis.
        {"start": 1.0, "end": 2.0, "avg_logprob": -0.22, "no_speech_prob": 0.02},
        {"start": 2.0, "end": 3.0, "text": "C", "avg_logprob": -0.33, "no_speech_prob": 0.03},
    ]}
    out = decompose_raw_json(raw)
    assert out["sentences"] == [[0.0, 1.0, "A"], [2.0, 3.0, "C"]]
    assert out["meta"]["avg_logprob"] == [-0.11, -0.33]
    assert out["meta"]["no_speech_prob"] == [0.01, 0.03]

    back = recompose_raw_json(out)
    pairs = [(s["text"], s.get("avg_logprob"), s.get("no_speech_prob")) for s in back["segments"]]
    assert pairs == [("A", -0.11, 0.01), ("C", -0.33, 0.03)], (
        f"metrics drifted onto the wrong sentences: {pairs}"
    )


def test_a_segment_without_metrics_does_not_shift_the_others():
    raw = {"segments": [
        {"start": 0.0, "end": 1.0, "text": "A", "avg_logprob": -0.11},
        {"start": 1.0, "end": 2.0, "text": "B"},
        {"start": 2.0, "end": 3.0, "text": "C", "avg_logprob": -0.33},
    ]}
    out = decompose_raw_json(raw)
    assert out["meta"]["avg_logprob"] == [-0.11, None, -0.33]
    back = recompose_raw_json(out)
    assert [s.get("avg_logprob") for s in back["segments"]] == [-0.11, None, -0.33]


# --------------------------------------------------------------- round trip

def test_recompose_feeds_the_word_level_consumer_unchanged():
    # merge_entries reads usable_words(raw). The recomposed payload must satisfy
    # it identically, or diarization silently changes behaviour.
    #
    # Compared on word/start/end/probability rather than whole dicts: t_dtw is
    # among the fields deliberately not stored (it is always -1 here), and
    # usable_words does not read it — it merely carries it through via {**word}.
    # Demanding it back would be demanding we keep the very field the task
    # exists to drop.
    from vts.services.diarization.merge import usable_words

    def _significant(words):
        return [
            (w.get("word"), w.get("start"), w.get("end"), w.get("probability"))
            for w in (words or [])
        ]

    original = _payload()
    back = recompose_raw_json(decompose_raw_json(original))
    assert _significant(usable_words(back)) == _significant(usable_words(original))
    # And the gluing really did run on the way out — the subword pieces came
    # back as one word, which is the behaviour diarization depends on.
    assert [w["word"] for w in usable_words(back)] == ["Привет", "мир"]


def test_recompose_feeds_the_sentence_level_consumer_unchanged():
    # The player and the subtitles read raw_json.segments[].start/end/text.
    from vts.services.player_transcript import _shifted_inner_sentences

    original = _payload()
    back = recompose_raw_json(decompose_raw_json(original))
    chunk_o = [{"start": 100.0, "raw_json": original}]
    chunk_b = [{"start": 100.0, "raw_json": back}]
    assert _shifted_inner_sentences(chunk_b) == _shifted_inner_sentences(chunk_o)


def test_empty_payload_decomposes_to_empty_axes():
    out = decompose_raw_json({})
    assert out["tokens"] == []
    assert out["sentences"] == []


def test_non_dict_payload_is_tolerated():
    # Never raise on stored data: a malformed row must degrade, not break the
    # task that reads it.
    assert decompose_raw_json(None)["tokens"] == []
    assert decompose_raw_json([1, 2, 3])["sentences"] == []


# ------------------------------------- round trip over the suite's own shapes

def test_round_trip_agrees_on_every_payload_shape_in_the_test_suite():
    """Both consumers must agree on payloads this module's author did not write.

    The fixtures above are hand-made and could easily encode the same
    assumptions as the implementation. This walks the AST of the rest of the
    test suite, pulls out every dict literal that looks like a whisper payload,
    and round-trips it: whatever shapes the project already exercises — subword
    tokens, silent chunks, backends without word timings — are checked against
    the real `usable_words` and `_shifted_inner_sentences`.
    """
    import ast
    from pathlib import Path

    from vts.services.diarization.merge import usable_words
    from vts.services.player_transcript import _shifted_inner_sentences

    def significant(words):
        return [
            (w.get("word"), w.get("start"), w.get("end"), w.get("probability"))
            for w in (words or [])
        ]

    checked = 0
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if "segments" not in keys:
                continue
            try:
                payload = ast.literal_eval(node)
            except (ValueError, SyntaxError, TypeError):
                continue
            segments = payload.get("segments")
            if not isinstance(segments, list) or not segments:
                continue
            if not all(isinstance(s, dict) for s in segments):
                continue

            checked += 1
            back = recompose_raw_json(decompose_raw_json(payload))
            assert significant(usable_words(back)) == significant(usable_words(payload)), (
                f"word axis diverged on a payload from {path.name}"
            )
            # A non-zero chunk offset, since that shift is where the sentence
            # axis would betray a lost or reordered record.
            before = _shifted_inner_sentences([{"start": 7.0, "raw_json": payload}])
            after = _shifted_inner_sentences([{"start": 7.0, "raw_json": back}])
            assert after == before, f"sentence axis diverged on a payload from {path.name}"

    # Guard the guard: if the harvesting ever stops finding payloads, this test
    # would pass vacuously and prove nothing.
    assert checked >= 20, f"only {checked} payloads harvested — the scan is not finding them"


# ------------------------------------------------- reading either stored form

def test_segment_payload_prefers_the_decomposed_axes():
    from types import SimpleNamespace

    from vts.services.asr_payload import segment_raw_payload

    original = _payload()
    seg = SimpleNamespace(payload=decompose_raw_json(original), raw_json={"segments": []})
    out = segment_raw_payload(seg)
    # The decomposed axes win: raw_json is legacy and will be cleared.
    assert out["segments"], "fell back to the empty legacy column"
    assert len(out["segments"]) == 2


def test_segment_payload_falls_back_to_raw_json_before_the_migration():
    from types import SimpleNamespace

    from vts.services.asr_payload import segment_raw_payload

    original = _payload()
    seg = SimpleNamespace(payload=None, raw_json=original)
    assert segment_raw_payload(seg) is original


def test_segment_payload_is_empty_when_neither_form_is_present():
    from types import SimpleNamespace

    from vts.services.asr_payload import segment_raw_payload

    assert segment_raw_payload(SimpleNamespace(payload=None, raw_json={})) == {}
    assert segment_raw_payload(SimpleNamespace(payload={}, raw_json={})) == {}

"""Word timestamps from whisper.cpp's server under --vad.

whisper-server maps each segment's start/end back onto the original audio, but
reports every word at its time in the audio LEFT AFTER silence removal. The
gap grows with every pause: measured on production chunks at typically 5-25 s
by the end of a five-minute chunk, 100 s after a long non-speech intro. Speaker
attribution walks the words, so a lawyer's answer was rendered under the
client's name because it fell into the client's question turn.

These tests build a response the way the server does: true word times, a
silence-removal map, words reported on the compressed timeline, segments on the
original one. Text is synthetic.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from vts.services.asr_payload import (
    decompose_raw_json,
    realign_word_times,
    recompose_raw_json,
    segment_raw_payload,
)
from vts.services.diarization.merge import merge_entries


# (text, true start, true end) per word; pauses fall BETWEEN segments.
_SEGMENTS = [
    [(" guten", 0.5, 0.9), (" morgen", 0.9, 1.4), (" zusammen", 1.4, 2.0)],
    [(" heute", 6.0, 6.4), (" geht", 6.4, 6.7), (" es", 6.7, 6.9), (" um", 6.9, 7.1), (" den", 7.1, 7.3), (" vertrag", 7.3, 8.0)],
    [(" haben", 15.0, 15.4), (" sie", 15.4, 15.6), (" fragen", 15.6, 16.2)],
    [(" ja", 24.0, 24.3), (" eine", 24.3, 24.6), (" frage", 24.6, 25.2), (" habe", 25.2, 25.5), (" ich", 25.5, 25.8)],
]
# Speech regions on the original timeline; everything between them is removed.
_REGIONS = [(0.4, 2.1), (5.9, 8.1), (14.9, 16.3), (23.9, 25.9)]


def _compress(t: float, regions=None) -> float:
    """Original time -> time in the audio with the silences cut out."""
    removed = 0.0
    previous_end = 0.0
    for start, end in regions or _REGIONS:
        if t < start:
            return t - removed - (t - previous_end)
        removed += start - previous_end
        if t <= end:
            return t - removed
        previous_end = end
    return t - removed


def _response(drifted: bool, regions=None) -> dict:
    segments = []
    for words in _SEGMENTS:
        segments.append({
            "start": words[0][1],
            "end": words[-1][2],
            "text": "".join(w for w, _, _ in words),
            "words": [
                {
                    "word": w,
                    "start": round(_compress(s, regions) if drifted else s, 3),
                    "end": round(_compress(e, regions) if drifted else e, 3),
                    "probability": 0.9,
                }
                for w, s, e in words
            ],
        })
    return {"language": "german", "duration": 30.0, "segments": segments}


def _word_times(payload: dict) -> list[tuple[str, float, float]]:
    return [(w["word"], w["start"], w["end"]) for s in payload["segments"] for w in s["words"]]


def _true_times() -> list[tuple[str, float, float]]:
    return [w for words in _SEGMENTS for w in words]


def _assert_close(got, want, tolerance=0.01):
    assert [w for w, _, _ in got] == [w for w, _, _ in want]
    for (word, gs, ge), (_, ws, we) in zip(got, want):
        assert abs(gs - ws) <= tolerance and abs(ge - we) <= tolerance, (word, gs, ge, ws, we)


def test_the_fixture_actually_drifts() -> None:
    # Guard the premise: the last word is reported 18.6 s early.
    last = _word_times(_response(drifted=True))[-1]
    assert last[0] == " ich"
    assert abs(last[2] - (25.8 - 18.6)) < 0.01


def test_drifted_words_are_moved_back_onto_the_original_timeline() -> None:
    _assert_close(_word_times(realign_word_times(_response(drifted=True))), _true_times())


def test_a_small_drift_is_corrected_too() -> None:
    """Short pauses drift words by a second or so. Few of them then fall
    outside their own segment, yet each can land on a neighbouring turn — a
    production chunk drifted up to 1.6 s with only 8% of words outside."""
    removed_per_pause = 0.1
    segments, truth, shift = [], [], 0.0
    t = 0.0
    for i in range(12):
        words = []
        for j in range(6):
            words.append((f" w{i}x{j}", t, t + 0.3))
            t += 0.3
        segments.append({
            "start": words[0][1], "end": words[-1][2], "text": "".join(w for w, _, _ in words),
            "words": [{"word": w, "start": round(a - shift, 3), "end": round(b - shift, 3)} for w, a, b in words],
        })
        truth += words
        t += 1.0  # a pause, of which VAD cuts a little
        shift += removed_per_pause
    response = {"segments": segments}
    assert 1.0 < shift < 1.5
    _assert_close(_word_times(realign_word_times(response)), truth)


def test_correct_word_times_are_left_exactly_as_they_are() -> None:
    """Another backend, or a whisper.cpp that maps word times itself, must
    pass through untouched — not nudged towards the segment edges."""
    response = _response(drifted=False)
    assert realign_word_times(copy.deepcopy(response)) == response


def test_realigning_twice_changes_nothing_more() -> None:
    once = realign_word_times(_response(drifted=True))
    assert realign_word_times(copy.deepcopy(once)) == once


def test_the_input_payload_is_not_modified() -> None:
    response = _response(drifted=True)
    before = copy.deepcopy(response)
    realign_word_times(response)
    assert response == before


def test_recompose_puts_drifted_words_back_under_their_own_sentence() -> None:
    """The stored form keeps words and sentences on separate axes. Joining
    them by time put a drifted word under an earlier sentence; the order of
    the text is what ties a word to its sentence."""
    payload = recompose_raw_json(decompose_raw_json(_response(drifted=True)))
    assert [[w["word"] for w in s["words"]] for s in payload["segments"]] == [
        [w for w, _, _ in words] for words in _SEGMENTS
    ]


def test_a_stored_row_reads_back_on_the_original_timeline() -> None:
    row = SimpleNamespace(payload=decompose_raw_json(_response(drifted=True)), raw_json={})
    _assert_close(_word_times(segment_raw_payload(row)), _true_times())


def test_a_legacy_row_reads_back_on_the_original_timeline() -> None:
    row = SimpleNamespace(payload=None, raw_json=_response(drifted=True))
    _assert_close(_word_times(segment_raw_payload(row)), _true_times())


def test_speakers_are_attributed_by_when_the_words_were_said() -> None:
    """What the user saw: the second speaker's question came out under the
    first speaker's name, because its words were reported 18 s early."""
    diarization = [
        {"start": 0.0, "end": 17.0, "speaker": "SPEAKER_00"},
        {"start": 23.5, "end": 26.5, "speaker": "SPEAKER_01"},
    ]
    row = SimpleNamespace(payload=decompose_raw_json(_response(drifted=True)), raw_json={})
    entries = [{"start": 0.0, "end": 26.0, "text": "whole chunk"}]
    merged = merge_entries(entries, {0: segment_raw_payload(row)}, diarization, min_words=2, min_seconds=0.8)
    by_speaker = {e["speaker"]: e["text"] for e in merged}
    assert by_speaker.get("SPEAKER_01") == "ja eine frage habe ich"

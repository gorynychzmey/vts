from vts.services.diarization.merge import speaker_at, usable_words

DIAR = [
    {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
    {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_01"},
]


def test_speaker_at_fully_inside_one_segment() -> None:
    assert speaker_at(DIAR, 1.0, 5.0) == "SPEAKER_00"


def test_speaker_at_picks_maximum_overlap() -> None:
    # 8.0-12.0: 2s overlap with SPEAKER_00, 2s with SPEAKER_01 — tie goes to the
    # earlier segment, keeping the result deterministic.
    assert speaker_at(DIAR, 8.0, 12.0) == "SPEAKER_00"
    # 9.0-14.0: 1s with SPEAKER_00, 4s with SPEAKER_01
    assert speaker_at(DIAR, 9.0, 14.0) == "SPEAKER_01"


def test_speaker_at_no_overlap_returns_none() -> None:
    assert speaker_at(DIAR, 30.0, 40.0) is None


def test_speaker_at_empty_diarization_returns_none() -> None:
    assert speaker_at([], 1.0, 5.0) is None


def test_usable_words_extracts_asr_words() -> None:
    raw = {
        "segments": [
            {
                "words": [
                    {"word": "привет", "start": 0.0, "end": 0.5},
                    {"word": "мир", "start": 0.5, "end": 1.0},
                ]
            }
        ]
    }
    words = usable_words(raw)
    assert words is not None
    assert [w["word"] for w in words] == ["привет", "мир"]


def test_usable_words_none_when_no_words() -> None:
    assert usable_words({"segments": [{"text": "нет слов"}]}) is None
    assert usable_words({}) is None


def test_usable_words_rejects_subword_fragments() -> None:
    # whisper.cpp emits subword tokens; splitting utterances on those would cut
    # words in half, so they must be rejected outright.
    raw = {
        "segments": [
            {
                "words": [
                    {"word": " к", "start": 0.0, "end": 0.1},
                    {"word": "от", "start": 0.1, "end": 0.2},
                    {"word": "ор", "start": 0.2, "end": 0.3},
                    {"word": "ые", "start": 0.3, "end": 0.4},
                ]
            }
        ]
    }
    assert usable_words(raw) is None


def test_usable_words_none_when_timestamps_missing() -> None:
    raw = {"segments": [{"words": [{"word": "привет"}]}]}
    assert usable_words(raw) is None

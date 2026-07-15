from vts.services.diarization.merge import speaker_at, usable_words, split_entry_by_speaker, merge_entries

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


TWO_SPEAKERS = [
    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
]


def test_split_entry_single_speaker_stays_one_entry() -> None:
    entry = {"start": 0.0, "end": 3.0, "text": "да я согласен полностью"}
    words = [
        {"word": "да", "start": 0.0, "end": 0.8},
        {"word": "я", "start": 0.8, "end": 1.4},
        {"word": "согласен", "start": 1.4, "end": 2.2},
        {"word": "полностью", "start": 2.2, "end": 3.0},
    ]
    result = split_entry_by_speaker(entry, words, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "да я согласен полностью"


def test_split_entry_real_turn_change_splits() -> None:
    # "да согласен" (SPEAKER_00, 0-4s) then "а ты что думаешь" (SPEAKER_01, 5-9s)
    entry = {"start": 0.0, "end": 9.0, "text": "да согласен а ты что думаешь"}
    words = [
        {"word": "да", "start": 0.0, "end": 2.0},
        {"word": "согласен", "start": 2.0, "end": 4.0},
        {"word": "а", "start": 5.0, "end": 6.0},
        {"word": "ты", "start": 6.0, "end": 7.0},
        {"word": "что", "start": 7.0, "end": 8.0},
        {"word": "думаешь", "start": 8.0, "end": 9.0},
    ]
    result = split_entry_by_speaker(entry, words, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 2
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "да согласен"
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 4.0
    assert result[1]["speaker"] == "SPEAKER_01"
    assert result[1]["text"] == "а ты что думаешь"
    assert result[1]["start"] == 5.0
    assert result[1]["end"] == 9.0


def test_split_entry_short_backchannel_absorbed() -> None:
    # "угу" from SPEAKER_01 mid-sentence: 1 word, 0.3s — below both thresholds,
    # so it must be absorbed by the PREVIOUS group and produce no entry.
    diar = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 2.3, "speaker": "SPEAKER_01"},
        {"start": 2.3, "end": 5.0, "speaker": "SPEAKER_00"},
    ]
    entry = {"start": 0.0, "end": 5.0, "text": "я думаю угу что это верно"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "угу", "start": 2.0, "end": 2.3},
        {"word": "что", "start": 2.3, "end": 3.0},
        {"word": "это", "start": 3.0, "end": 4.0},
        {"word": "верно", "start": 4.0, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "я думаю угу что это верно"


def test_split_entry_group_below_word_threshold_absorbed() -> None:
    # A group that is long enough in seconds but only 1 word fails the AND.
    diar = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
        {"start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
    ]
    entry = {"start": 0.0, "end": 4.0, "text": "я думаю дааааа"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "дааааа", "start": 2.0, "end": 4.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"


def test_split_entry_first_group_below_threshold_absorbed_forward() -> None:
    # No previous group to absorb into — the leading fragment joins the next one.
    diar = [
        {"start": 0.0, "end": 0.3, "speaker": "SPEAKER_01"},
        {"start": 0.3, "end": 5.0, "speaker": "SPEAKER_00"},
    ]
    entry = {"start": 0.0, "end": 5.0, "text": "угу я думаю что это верно"}
    words = [
        {"word": "угу", "start": 0.0, "end": 0.3},
        {"word": "я", "start": 0.3, "end": 1.5},
        {"word": "думаю", "start": 1.5, "end": 3.0},
        {"word": "что", "start": 3.0, "end": 4.0},
        {"word": "это", "start": 4.0, "end": 4.5},
        {"word": "верно", "start": 4.5, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "угу я думаю что это верно"


def test_split_entry_unattributed_words_join_previous() -> None:
    # Words outside any diarization segment must not vanish.
    diar = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}]
    entry = {"start": 0.0, "end": 5.0, "text": "я думаю что это верно"}
    words = [
        {"word": "я", "start": 0.0, "end": 1.0},
        {"word": "думаю", "start": 1.0, "end": 2.0},
        {"word": "что", "start": 3.0, "end": 3.5},
        {"word": "это", "start": 3.5, "end": 4.0},
        {"word": "верно", "start": 4.0, "end": 5.0},
    ]
    result = split_entry_by_speaker(entry, words, diar, min_words=2, min_seconds=0.8)
    assert len(result) == 1
    assert result[0]["text"] == "я думаю что это верно"
    assert result[0]["speaker"] == "SPEAKER_00"


def test_merge_entries_without_words_uses_max_overlap() -> None:
    # The cpp path: no usable words, so each entry gets one speaker as a whole.
    entries = [
        {"start": 0.0, "end": 4.0, "text": "первая фраза"},
        {"start": 6.0, "end": 9.0, "text": "вторая фраза"},
    ]
    result = merge_entries(entries, {}, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert [e["text"] for e in result] == ["первая фраза", "вторая фраза"]


def test_merge_entries_splits_using_words() -> None:
    entries = [{"start": 0.0, "end": 9.0, "text": "да согласен а ты что думаешь"}]
    raw_by_index = {
        0: {
            "segments": [
                {
                    "words": [
                        {"word": "да", "start": 0.0, "end": 2.0},
                        {"word": "согласен", "start": 2.0, "end": 4.0},
                        {"word": "а", "start": 5.0, "end": 6.0},
                        {"word": "ты", "start": 6.0, "end": 7.0},
                        {"word": "что", "start": 7.0, "end": 8.0},
                        {"word": "думаешь", "start": 8.0, "end": 9.0},
                    ]
                }
            ]
        }
    }
    result = merge_entries(entries, raw_by_index, TWO_SPEAKERS, min_words=2, min_seconds=0.8)
    assert len(result) == 2
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]


def test_merge_entries_empty_diarization_leaves_speaker_none() -> None:
    entries = [{"start": 0.0, "end": 4.0, "text": "фраза"}]
    result = merge_entries(entries, {}, [], min_words=2, min_seconds=0.8)
    assert result[0]["speaker"] is None

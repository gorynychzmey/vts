from vts.pipeline.steps.transcription import trim_repetitive_edges
from vts.services.diarization.merge import (
    auto_noise_labels,
    merge_entries,
    nearest_speaker,
    speaker_at,
    speaker_shares,
    split_entry_by_speaker,
    trim_repetitive_entries,
    usable_words,
)

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
    # speaker_at answers "who is speaking during this span" — silence has no
    # answer. Filling the gap is nearest_speaker's job, kept separate on purpose.
    assert speaker_at(DIAR, 30.0, 40.0) is None


def test_nearest_speaker_prefers_overlap() -> None:
    # When there IS overlap, nearest_speaker matches speaker_at exactly.
    assert nearest_speaker(DIAR, 1.0, 5.0) == "SPEAKER_00"
    assert nearest_speaker(DIAR, 9.0, 14.0) == "SPEAKER_01"


def test_nearest_speaker_fills_gap_toward_closer_edge() -> None:
    # The real bug: a word in the 10.0-boundaryless gap between two speakers.
    # DIAR is 0-10 SPEAKER_00, 10-20 SPEAKER_01 — contiguous, so make a gap.
    gapped = [
        {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
        {"start": 12.0, "end": 20.0, "speaker": "SPEAKER_01"},
    ]
    # 10.4 is 0.4 past SPEAKER_00's end, 1.6 before SPEAKER_01 -> SPEAKER_00.
    assert nearest_speaker(gapped, 10.4, 10.4) == "SPEAKER_00"
    # 11.7 is 1.7 past SPEAKER_00, 0.3 before SPEAKER_01 -> SPEAKER_01. This is
    # the "в принципе" case: the word belongs to the speaker about to start.
    assert nearest_speaker(gapped, 11.7, 11.7) == "SPEAKER_01"


def test_nearest_speaker_none_on_empty() -> None:
    assert nearest_speaker([], 5.0, 5.0) is None


def test_speaker_at_zero_length_word_inside_segment() -> None:
    # Whisper emits zero-length words (a real one: "в" at 21.28-21.28). Area of
    # overlap is 0 for those, so an area-only rule never attributes them — not
    # even in the middle of a confident segment. 3% of this meeting's words.
    assert speaker_at(DIAR, 5.0, 5.0) == "SPEAKER_00"
    assert speaker_at(DIAR, 15.0, 15.0) == "SPEAKER_01"


def test_speaker_at_zero_length_word_outside_any_segment() -> None:
    assert speaker_at(DIAR, 30.0, 30.0) is None


def test_speaker_at_zero_length_word_on_boundary() -> None:
    # Exactly on the 10.0 boundary: both segments touch it, so the earlier one
    # wins, matching the tie rule for non-zero spans.
    assert speaker_at(DIAR, 10.0, 10.0) == "SPEAKER_00"


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


def test_usable_words_glues_subword_fragments() -> None:
    # whisper.cpp splits words into subword tokens; a token without a leading
    # space continues the previous one. Gluing them recovers whole words with
    # exact boundaries — the first token's start, the last one's end.
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
    words = usable_words(raw)
    assert words is not None
    assert [w["word"] for w in words] == ["которые"]
    assert words[0]["start"] == 0.0
    assert words[0]["end"] == 0.4


def test_usable_words_glues_real_cpp_payload() -> None:
    # Verbatim from a real whisper.cpp task (c31487fb, 2026-07-05): "прогоняет"
    # and "иишку." arrive in pieces, and t_dtw marks the cpp backend.
    raw = {
        "segments": [
            {
                "words": [
                    {"word": " и", "start": 0.0, "end": 0.09, "t_dtw": -1},
                    {"word": " прог", "start": 0.09, "end": 0.42, "t_dtw": -1},
                    {"word": "он", "start": 0.53, "end": 0.62, "t_dtw": -1},
                    {"word": "яет", "start": 0.65, "end": 0.9, "t_dtw": -1},
                    {"word": " через", "start": 0.9, "end": 1.35, "t_dtw": -1},
                    {"word": " и", "start": 1.35, "end": 1.44, "t_dtw": -1},
                    {"word": "иш", "start": 1.44, "end": 1.62, "t_dtw": -1},
                    {"word": "ку", "start": 1.62, "end": 1.76, "t_dtw": -1},
                    {"word": ".", "start": 1.76, "end": 1.87, "t_dtw": -1},
                ]
            }
        ]
    }
    words = usable_words(raw)
    assert words is not None
    assert [w["word"] for w in words] == ["и", "прогоняет", "через", "иишку."]
    # "прогоняет" spans its first token's start to its last token's end.
    assert words[1]["start"] == 0.09
    assert words[1]["end"] == 0.9


def test_usable_words_leaves_whole_words_alone() -> None:
    # The asr backend already emits whole words; gluing must not merge them.
    raw = {
        "segments": [
            {
                "words": [
                    {"word": " привет", "start": 0.0, "end": 0.5},
                    {"word": " мир", "start": 0.5, "end": 1.0},
                ]
            }
        ]
    }
    words = usable_words(raw)
    assert words is not None
    assert [w["word"] for w in words] == ["привет", "мир"]


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


# --- Finding 1: sentence-granularity trimming on the entry list -------------
#
# Production entries are ~300s ASR chunks (segment_target_seconds), each one
# containing dozens of sentences — never one sentence per entry. These tests
# pin that trim_repetitive_entries agrees with trim_repetitive_edges (the flat
# path) on what gets removed, at that realistic shape and at shapes smaller
# than min_repeats=6 entries where the old whole-entry-as-unit approach could
# never even start its loop.

_HALLUCINATION = "Продолжение следует."


def _entries_from_texts(texts: list[str], *, seconds_each: float = 300.0) -> list[dict]:
    entries = []
    for i, text in enumerate(texts):
        entries.append(
            {
                "start": i * seconds_each,
                "end": (i + 1) * seconds_each,
                "text": text,
                "speaker": "SPEAKER_00",
            }
        )
    return entries


def _flat_text(entries: list[dict]) -> str:
    return " ".join(str(e["text"]).strip() for e in entries if str(e["text"]).strip())


def test_realistic_shape_diarized_and_flat_remove_identical_content() -> None:
    # 3 entries of 300s, matching segment_target_seconds; the last one is a
    # single 5-minute ASR chunk containing 30 looped hallucination sentences
    # (silence at the tail of a 15-minute video) plus nothing else.
    real_speech_1 = "Привет всем. Сегодня поговорим о важном."
    real_speech_2 = "Продолжаем обсуждение основной темы. Это было интересно."
    tail_hallucination = " ".join([_HALLUCINATION] * 30)

    entries = _entries_from_texts([real_speech_1, real_speech_2, tail_hallucination])
    flat_input = _flat_text(entries)

    diarized_kept, diarized_meta = trim_repetitive_entries(entries)
    flat_cleaned, flat_meta = trim_repetitive_edges(flat_input)

    # The property under test: same input, same content removed.
    diarized_text = _flat_text(diarized_kept)
    assert diarized_text == flat_cleaned
    assert diarized_meta["removed_head_sentences"] == flat_meta["removed_head_sentences"]
    assert diarized_meta["removed_tail_sentences"] == flat_meta["removed_tail_sentences"]
    # And concretely: all 30 hallucinated sentences must be gone, not 0.
    assert flat_meta["removed_tail_sentences"] == 30
    assert diarized_meta["removed_tail_sentences"] == 30
    assert _HALLUCINATION not in diarized_text


def test_two_entries_four_repeats_each_below_old_entry_count_threshold() -> None:
    # Only 2 entries total — the old whole-entry-as-unit code could never
    # reach min_repeats=6 regardless of content. At sentence granularity the
    # 8 repeated sentences (4 in each entry) clear the threshold.
    entries = _entries_from_texts(
        [
            " ".join([_HALLUCINATION] * 4),
            " ".join([_HALLUCINATION] * 4) + " Настоящая речь начинается здесь.",
        ]
    )
    flat_input = _flat_text(entries)

    diarized_kept, diarized_meta = trim_repetitive_entries(entries)
    flat_cleaned, flat_meta = trim_repetitive_edges(flat_input)

    assert _flat_text(diarized_kept) == flat_cleaned
    assert diarized_meta["removed_head_sentences"] == flat_meta["removed_head_sentences"] == 8
    assert diarized_meta["removed_tail_sentences"] == flat_meta["removed_tail_sentences"] == 0
    assert _flat_text(diarized_kept) == "Настоящая речь начинается здесь."


def test_five_entries_two_hallucination_sentences_each() -> None:
    # 5 entries (still under the old 6-entry floor), 2 hallucination sentences
    # apiece = 10 total, well past min_repeats at sentence granularity. All
    # 10 sentences are hallucination -> nothing survives -> the all-repeats
    # safety net falls back to the untrimmed original (same contract as
    # trim_repetitive_edges: never show empty text), while meta still reports
    # what WOULD have been removed.
    entries = _entries_from_texts([" ".join([_HALLUCINATION] * 2) for _ in range(5)])
    flat_input = _flat_text(entries)

    diarized_kept, diarized_meta = trim_repetitive_entries(entries)
    flat_cleaned, flat_meta = trim_repetitive_edges(flat_input)

    assert _flat_text(diarized_kept) == flat_cleaned == flat_input
    assert diarized_meta["removed_head_sentences"] == flat_meta["removed_head_sentences"] == 10
    assert diarized_kept == entries


def test_one_entry_mixing_hallucination_and_real_speech() -> None:
    # A single entry containing 8 hallucination sentences followed by real
    # speech: the old code treated the whole entry as one unit and could
    # never trim anything out of it. At sentence granularity the run of 8
    # matching sentences at the head is caught, and the real speech survives.
    text = " ".join([_HALLUCINATION] * 8) + " Настоящая речь продолжается."
    entries = _entries_from_texts([text])
    flat_input = _flat_text(entries)

    diarized_kept, diarized_meta = trim_repetitive_entries(entries)
    flat_cleaned, flat_meta = trim_repetitive_edges(flat_input)

    assert _flat_text(diarized_kept) == flat_cleaned
    assert diarized_meta["removed_head_sentences"] == flat_meta["removed_head_sentences"] == 8
    assert _flat_text(diarized_kept) == "Настоящая речь продолжается."
    # The entry survives (not dropped) because it still has a surviving sentence.
    assert len(diarized_kept) == 1
    assert diarized_kept[0]["speaker"] == "SPEAKER_00"


def test_trim_repetitive_entries_preserves_speaker_start_end_per_sentence() -> None:
    # A trimmed run can straddle an entry boundary; surviving sentences must
    # keep THEIR OWN entry's speaker/start/end, not bleed into a neighbour's.
    entries = [
        {"start": 0.0, "end": 300.0, "text": "Настоящая речь первого спикера.", "speaker": "SPEAKER_00"},
        {
            "start": 300.0,
            "end": 600.0,
            "text": " ".join([_HALLUCINATION] * 6),
            "speaker": "SPEAKER_01",
        },
    ]
    kept, meta = trim_repetitive_entries(entries)
    assert meta["removed_tail_sentences"] == 6
    assert len(kept) == 1
    assert kept[0]["text"] == "Настоящая речь первого спикера."
    assert kept[0]["speaker"] == "SPEAKER_00"
    assert kept[0]["start"] == 0.0
    assert kept[0]["end"] == 300.0


def test_trim_repetitive_entries_empty_list() -> None:
    kept, meta = trim_repetitive_entries([])
    assert kept == []
    assert meta == {
        "removed_head_sentences": 0,
        "removed_tail_sentences": 0,
        "head_phrase": None,
        "tail_phrase": None,
    }


def test_trim_repetitive_entries_no_repeats_is_noop() -> None:
    entries = _entries_from_texts(["Первая речь. Вторая мысль.", "Третья мысль тут."])
    kept, meta = trim_repetitive_entries(entries)
    assert kept == entries
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


def test_speaker_shares_by_diarization_time():
    segs = [
        {"start": 0.0, "end": 10.0, "speaker": "A"},
        {"start": 10.0, "end": 12.0, "speaker": "B"},
        {"start": 12.0, "end": 20.0, "speaker": "A"},
    ]
    shares = speaker_shares(segs)
    # A = 18s, B = 2s, total 20s
    assert abs(shares["A"] - 0.9) < 1e-9
    assert abs(shares["B"] - 0.1) < 1e-9


def test_speaker_shares_empty():
    assert speaker_shares([]) == {}


def test_speaker_seconds_by_diarization_time():
    from vts.services.diarization.merge import speaker_seconds
    segs = [
        {"start": 0.0, "end": 10.0, "speaker": "A"},
        {"start": 10.0, "end": 12.0, "speaker": "B"},
        {"start": 12.0, "end": 20.0, "speaker": "A"},
    ]
    secs = speaker_seconds(segs)
    # A = 18s of actual speech, B = 2s — real diarized seconds, NOT scaled by
    # media length (which includes silence). This is what the UI shows.
    assert abs(secs["A"] - 18.0) < 1e-9
    assert abs(secs["B"] - 2.0) < 1e-9


def test_speaker_seconds_empty():
    from vts.services.diarization.merge import speaker_seconds
    assert speaker_seconds([]) == {}


def test_auto_noise_close_and_small_is_noise():
    shares = {"A": 0.95, "B": 0.05}
    # B is tiny AND its embedding is identical to A -> echo -> noise
    emb = {"A": [1.0, 0.0], "B": [1.0, 0.0]}
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == {"B"}


def test_auto_noise_far_and_small_is_not_noise():
    shares = {"A": 0.95, "B": 0.05}
    # B is tiny but acoustically distinct from A (orthogonal -> cosine dist 1.0)
    emb = {"A": [1.0, 0.0], "B": [0.0, 1.0]}
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()


def test_auto_noise_large_speaker_never_noise():
    shares = {"A": 0.60, "B": 0.40}
    emb = {"A": [1.0, 0.0], "B": [1.0, 0.0]}  # identical, but B is large-share
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()


def test_auto_noise_no_embedding_never_noise():
    shares = {"A": 0.95, "B": 0.05}
    emb = {"A": [1.0, 0.0]}  # B has no embedding
    assert auto_noise_labels(shares, emb, min_share=0.10, max_distance=0.25) == set()

import json
from pathlib import Path

from vts.pipeline.steps.transcription import apply_diarization


def test_no_diarization_file_leaves_entries_untouched(tmp_path: Path) -> None:
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text, cleanup_meta = apply_diarization(
        entries,
        {},
        tmp_path / "missing.json",
        min_words=2,
        min_seconds=0.8,
        min_share=0.05,
    )
    # Zero regression: same entries, no speaker key, text joined as before.
    assert result == entries
    assert text is None
    assert cleanup_meta is None


def test_diarization_file_adds_speakers_and_renders(tmp_path: Path) -> None:
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ],
                "embeddings": {},
                "num_speakers": 2,
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "привет"},
        {"start": 6.0, "end": 9.0, "text": "здравствуй"},
    ]
    result, text, cleanup_meta = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    # Technical tags in the data; "Голос N" only in the rendered text.
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert text == "Голос 1: привет\n\nГолос 2: здравствуй"
    # No hallucination repeats here, so cleanup is a no-op — but it must still
    # be reported (not None) since diarization did run.
    assert cleanup_meta == {
        "removed_head_sentences": 0,
        "removed_tail_sentences": 0,
        "head_phrase": None,
        "tail_phrase": None,
    }


def test_single_speaker_renders_flat(tmp_path: Path) -> None:
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {"segments": [{"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"}], "num_speakers": 1}
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "первая"},
        {"start": 4.0, "end": 9.0, "text": "вторая"},
    ]
    result, text, cleanup_meta = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_00"]
    assert text == "первая вторая"
    assert cleanup_meta is not None


def test_corrupt_diarization_file_degrades_to_no_speakers(tmp_path: Path) -> None:
    # A broken artifact must not fail the whole task — the transcript is the
    # valuable output; speaker labels are an enhancement.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text("{not json", encoding="utf-8")
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text, cleanup_meta = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result == entries
    assert text is None
    assert cleanup_meta is None


def test_empty_text_chunk_does_not_shift_word_attribution(tmp_path: Path) -> None:
    # A chunk with empty text produces no entry. Building the word map over the
    # unfiltered chunk list would shift every later entry onto another chunk's
    # words and scatter speakers at random.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # Entry 0 comes from chunk 1 (chunk 0 was silent), so its words are chunk 1's.
    entries = [{"start": 6.0, "end": 9.0, "text": "привет мир"}]
    raw_by_index = {
        0: {
            "segments": [
                {
                    "words": [
                        {"word": "привет", "start": 6.0, "end": 7.0},
                        {"word": "мир", "start": 7.0, "end": 9.0},
                    ]
                }
            ]
        }
    }
    result, _, _ = apply_diarization(
        entries, raw_by_index, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result[0]["speaker"] == "SPEAKER_01"


def test_diarized_path_strips_leading_hallucination_entries(tmp_path: Path) -> None:
    # Finding 1: rendering must come from CLEANED entries, not raw ones. A
    # Whisper hallucination ("Субтитры сделал DimaTorzok.") repeated 8 times at
    # the head must be gone from both the rendered text AND from `result`
    # (whoever reads `entries` downstream must see the same content a human
    # reading the transcript sees).
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 100.0, "speaker": "SPEAKER_00"}]}),
        encoding="utf-8",
    )
    hallucination = "Субтитры сделал DimaTorzok."
    entries = [{"start": float(i), "end": float(i + 1), "text": hallucination} for i in range(8)]
    entries.append({"start": 10.0, "end": 15.0, "text": "настоящая речь начинается здесь"})

    result, text, cleanup_meta = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )

    assert text == "настоящая речь начинается здесь"
    assert [e["text"] for e in result] == ["настоящая речь начинается здесь"]
    # The metadata must describe what was actually removed from the diarized
    # output — not be silently borrowed from a flat-text pass that never ran.
    assert cleanup_meta["removed_head_sentences"] == 8
    assert cleanup_meta["removed_tail_sentences"] == 0


def test_diarized_path_entries_and_text_agree_on_marginal_speaker(tmp_path: Path) -> None:
    # Finding 3: a 1s marginal SPEAKER_01 in a much longer clip must be folded
    # into the dominant speaker in BOTH the rendered text (already true before
    # the fix, via render_transcript's internal drop_marginal_speakers) AND the
    # returned `entries` (was NOT true before the fix — `merged` was returned
    # pre-reassignment). vts-80i will read entries[i]["speaker"] to name real
    # people; it must never see a phantom speaker the rendered text hid.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 20.0, "speaker": "SPEAKER_00"},
                    {"start": 20.0, "end": 21.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 20.0, "text": "долгий монолог"},
        {"start": 20.0, "end": 21.0, "text": "шум"},
    ]

    result, text, _ = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )

    # Rendered as flat text: only one real speaker.
    assert text == "долгий монолог шум"
    # entries must agree: no SPEAKER_01 surviving anywhere.
    assert all(e["speaker"] == "SPEAKER_00" for e in result)


def test_english_recording_gets_speaker_labels_not_golos(tmp_path: Path) -> None:
    # Finding 1: the label word must match the recording's language, or
    # segment_prompt.md's "Output MUST be in English" instruction directly
    # contradicts "keep Голос 1: exactly as it appears" for every non-Russian
    # meeting. entries[i]["speaker"] must still carry the TECHNICAL tag
    # unchanged (vts-80i, speaker enrollment, looks those up) -- only the
    # rendered label depends on language.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "hello there"},
        {"start": 6.0, "end": 9.0, "text": "good morning"},
    ]
    result, text, _ = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05, language="en"
    )
    assert text == "Speaker 1: hello there\n\nSpeaker 2: good morning"
    # Technical tags untouched by language -- render-time concern only.
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]


def test_english_diarized_transcript_still_splits_into_utterances(tmp_path: Path) -> None:
    # End-to-end proof of the Task 7 coupling: split_utterances (summarizer.py)
    # must recognize "Speaker N:" labels, not just "Голос N:", or an English
    # diarized transcript renders correctly but silently stops chunking on
    # utterance boundaries -- the labels become invisible to the splitter.
    from vts.services.summarizer import split_utterances

    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "hello there, how are you"},
        {"start": 6.0, "end": 9.0, "text": "good morning to you too"},
    ]
    _, text, _ = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05, language="en"
    )
    assert text is not None

    utterances = split_utterances(text)
    assert utterances == [
        "Speaker 1: hello there, how are you",
        "Speaker 2: good morning to you too",
    ]
    assert len(utterances) > 1  # the coupling this test exists to catch


def test_apply_diarization_substitutes_registry_names(tmp_path: Path) -> None:
    """A matched voice renders as the person's name; an unmatched one keeps
    its numbered label and its position in the numbering."""
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ],
                "embeddings": {},
                "num_speakers": 2,
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "привет"},
        {"start": 6.0, "end": 9.0, "text": "здравствуй"},
    ]
    result, text, _ = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05,
        names={"SPEAKER_00": "Вася"},
    )
    # Technical tags survive in the data — substitution is render-time only.
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert text == "Вася: привет\n\nГолос 2: здравствуй"


def test_apply_diarization_without_names_is_unchanged(tmp_path: Path) -> None:
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
                ],
                "num_speakers": 2,
            }
        ),
        encoding="utf-8",
    )
    entries = [
        {"start": 0.0, "end": 4.0, "text": "привет"},
        {"start": 6.0, "end": 9.0, "text": "здравствуй"},
    ]
    _, text, _ = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05,
    )
    assert text == "Голос 1: привет\n\nГолос 2: здравствуй"

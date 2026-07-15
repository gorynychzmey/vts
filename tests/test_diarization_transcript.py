import json
from pathlib import Path

from vts.pipeline.steps.transcription import apply_diarization


def test_no_diarization_file_leaves_entries_untouched(tmp_path: Path) -> None:
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text = apply_diarization(
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
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    # Technical tags in the data; "Голос N" only in the rendered text.
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_01"]
    assert text == "Голос 1: привет\n\nГолос 2: здравствуй"


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
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert [e["speaker"] for e in result] == ["SPEAKER_00", "SPEAKER_00"]
    assert text == "первая вторая"


def test_corrupt_diarization_file_degrades_to_no_speakers(tmp_path: Path) -> None:
    # A broken artifact must not fail the whole task — the transcript is the
    # valuable output; speaker labels are an enhancement.
    diar_path = tmp_path / "diarization.json"
    diar_path.write_text("{not json", encoding="utf-8")
    entries = [{"start": 0.0, "end": 5.0, "text": "первая"}]
    result, text = apply_diarization(
        entries, {}, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result == entries
    assert text is None


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
    result, _ = apply_diarization(
        entries, raw_by_index, diar_path, min_words=2, min_seconds=0.8, min_share=0.05
    )
    assert result[0]["speaker"] == "SPEAKER_01"

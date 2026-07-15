import pytest

from vts.services.diarization import create_diarization_backend
from vts.services.diarization._pyannote import PyannoteBackend


def _pyannote() -> PyannoteBackend:
    return PyannoteBackend("http://localhost")


def test_create_backend_returns_pyannote() -> None:
    backend = create_diarization_backend("http://localhost", "pyannote")
    assert isinstance(backend, PyannoteBackend)
    assert backend.backend_name == "pyannote"


def test_create_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown diarization backend"):
        create_diarization_backend("http://localhost", "nope")


def test_normalize_output_extracts_segments_and_embeddings() -> None:
    payload = {
        "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
        "embeddings": {"SPEAKER_00": [0.1, 0.2]},
        "num_speakers": 1,
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    assert result["embeddings"] == {"SPEAKER_00": [0.1, 0.2]}
    assert result["num_speakers"] == 1


def test_normalize_output_defaults_when_fields_missing() -> None:
    result = _pyannote().normalize_output({})
    assert result == {"segments": [], "embeddings": {}, "num_speakers": 0}


def test_normalize_output_drops_malformed_segments() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "speaker": "SPEAKER_01"},
            "garbage",
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]


def test_normalize_output_infers_num_speakers() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
        ]
    }
    assert _pyannote().normalize_output(payload)["num_speakers"] == 2


def test_normalize_output_infers_num_speakers_counts_distinct() -> None:
    # 3 segments but only 2 distinct speakers: pins distinct-speaker-count
    # semantics against a segment-count mutation (len(segments) would give 3).
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_01"},
            {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_00"},
        ]
    }
    assert _pyannote().normalize_output(payload)["num_speakers"] == 2


def test_normalize_output_segments_not_a_list_is_treated_as_empty() -> None:
    # The sidecar is untrusted input; a wrong type must not be iterated.
    result = _pyannote().normalize_output({"segments": 5})
    assert result["segments"] == []
    assert result["num_speakers"] == 0


def test_normalize_output_segments_as_dict_is_treated_as_empty() -> None:
    result = _pyannote().normalize_output({"segments": {"start": 0.0}})
    assert result["segments"] == []


def test_normalize_output_segments_as_string_is_treated_as_empty() -> None:
    result = _pyannote().normalize_output({"segments": "garbage"})
    assert result["segments"] == []


def test_normalize_output_drops_segment_with_non_numeric_start() -> None:
    payload = {
        "segments": [
            {"start": "abc", "end": "5.0", "speaker": "SPEAKER_00"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == []


def test_normalize_output_drops_segment_with_non_numeric_end() -> None:
    payload = {
        "segments": [
            {"start": 0.0, "end": "not-a-number", "speaker": "SPEAKER_00"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == []


def test_normalize_output_keeps_good_segments_alongside_dropped_ones() -> None:
    # The actual promise: one bad segment must not take good ones down with it.
    payload = {
        "segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": "abc", "end": "5.0", "speaker": "SPEAKER_01"},
            {"start": 5.0, "end": "bad", "speaker": "SPEAKER_02"},
            {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_03"},
        ]
    }
    result = _pyannote().normalize_output(payload)
    assert result["segments"] == [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_03"},
    ]


def test_normalize_output_warns_when_every_segment_is_dropped(caplog) -> None:
    # Dropping is silent by design, so a systematically broken sidecar would
    # look like a quiet monologue. The log is what makes it visible.
    payload = {"segments": [{"start": "abc", "end": "x", "speaker": "SPEAKER_00"}]}
    with caplog.at_level("WARNING"):
        result = _pyannote().normalize_output(payload)
    assert result["segments"] == []
    assert "none survived normalization" in caplog.text


def test_normalize_output_silent_when_genuinely_empty(caplog) -> None:
    # An empty segments list is not evidence of a broken response — no warning.
    with caplog.at_level("WARNING"):
        _pyannote().normalize_output({"segments": []})
    assert caplog.text == ""

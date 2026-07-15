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

from vts.services.transcription._asr import AsrBackend
from vts.services.transcription._cpp import CppBackend


def _asr() -> AsrBackend:
    return AsrBackend("http://localhost")


def _cpp() -> CppBackend:
    return CppBackend("http://localhost")


def test_normalize_output_strips_text() -> None:
    payload = {"text": "  Hello world  "}
    assert _asr().normalize_output(payload) == "Hello world"
    assert _cpp().normalize_output(payload) == "Hello world"


def test_normalize_output_empty_text() -> None:
    assert _asr().normalize_output({}) == ""
    assert _cpp().normalize_output({}) == ""


def test_normalize_output_ignores_subword_tokens() -> None:
    # whisper.cpp returns subword tokens in `words`; they must not affect the result
    payload = {
        "text": "который в момент определяют",
        "segments": [
            {
                "words": [
                    {"word": " к", "start": 0.0, "end": 0.1, "probability": 0.9},
                    {"word": "от", "start": 0.1, "end": 0.2, "probability": 0.9},
                    {"word": "ор", "start": 0.2, "end": 0.3, "probability": 0.9},
                    {"word": "ые", "start": 0.3, "end": 0.4, "probability": 0.9},
                ]
            }
        ],
    }
    assert _cpp().normalize_output(payload) == "который в момент определяют"
    assert _asr().normalize_output(payload) == "который в момент определяют"

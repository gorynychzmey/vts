from vts.services.transcription._asr import AsrBackend
from vts.services.transcription._cpp import CppBackend


def _asr() -> AsrBackend:
    return AsrBackend("http://localhost")


def _cpp() -> CppBackend:
    return CppBackend("http://localhost")


def test_cpp_normalize_output_uses_text_field() -> None:
    payload = {
        "text": " который в момент определяют, можем ли мы остановиться",
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
    text, words = _cpp().normalize_output(payload, segment_offset_sec=0.0)
    assert text == "который в момент определяют, можем ли мы остановиться"
    assert words == []


def test_cpp_normalize_output_returns_no_words() -> None:
    payload = {"text": "Hello world", "segments": [{"words": [{"word": "Hello", "start": 0.0, "end": 0.5}]}]}
    _, words = _cpp().normalize_output(payload, segment_offset_sec=0.0)
    assert words == []


def test_asr_normalize_output_builds_text_from_payload() -> None:
    payload = {
        "text": "Hello world",
        "segments": [
            {
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 0.4, "probability": 0.95},
                    {"word": "world", "start": 0.5, "end": 0.9, "probability": 0.98},
                ]
            }
        ],
    }
    text, words = _asr().normalize_output(payload, segment_offset_sec=5.0)
    assert text == "Hello world"
    assert len(words) == 2
    assert words[0]["word"] == "Hello"
    assert words[0]["start"] == 5.0
    assert words[1]["start"] == 5.5


def test_asr_normalize_output_applies_segment_offset() -> None:
    payload = {
        "text": "test",
        "segments": [{"words": [{"word": "test", "start": 1.0, "end": 2.0, "probability": 0.9}]}],
    }
    _, words = _asr().normalize_output(payload, segment_offset_sec=10.0)
    assert words[0]["start"] == 11.0
    assert words[0]["end"] == 12.0

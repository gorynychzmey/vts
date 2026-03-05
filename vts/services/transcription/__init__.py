from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ._asr import transcribe_asr
from ._cpp import detect_language_with_cpp, transcribe_cpp

WhisperBackend = Literal["asr", "cpp"]


async def transcribe_with_whisper(
    *,
    whisper_url: str,
    whisper_backend: WhisperBackend = "asr",
    audio_path: Path,
    language: str | None,
    initial_prompt: str | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    if whisper_backend == "cpp":
        return await transcribe_cpp(
            whisper_url=whisper_url,
            audio_path=audio_path,
            language=language,
            initial_prompt=initial_prompt,
            timeout_seconds=timeout_seconds,
        )
    return await transcribe_asr(
        whisper_url=whisper_url,
        audio_path=audio_path,
        language=language,
        initial_prompt=initial_prompt,
        timeout_seconds=timeout_seconds,
    )


def normalize_whisper_output(
    payload: dict[str, Any],
    *,
    segment_offset_sec: float,
    whisper_backend: WhisperBackend = "asr",
) -> tuple[str, list[dict[str, Any]]]:
    raw_segments = payload.get("segments", [])
    text = str(payload.get("text", "")).strip()
    words: list[dict[str, Any]] = []
    for seg in raw_segments if isinstance(raw_segments, list) else []:
        if not isinstance(seg, dict):
            continue
        for word in seg.get("words", []):
            if not isinstance(word, dict):
                continue
            words.append(
                {
                    "word": str(word.get("word", "")).strip(),
                    "start": float(word.get("start", 0.0)) + segment_offset_sec,
                    "end": float(word.get("end", 0.0)) + segment_offset_sec,
                    "confidence": word.get("probability"),
                }
            )
    return text, words


__all__ = [
    "WhisperBackend",
    "detect_language_with_cpp",
    "normalize_whisper_output",
    "transcribe_with_whisper",
]

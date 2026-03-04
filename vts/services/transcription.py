from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import httpx

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
        return await _transcribe_cpp(
            whisper_url=whisper_url,
            audio_path=audio_path,
            language=language,
            initial_prompt=initial_prompt,
            timeout_seconds=timeout_seconds,
        )
    return await _transcribe_asr(
        whisper_url=whisper_url,
        audio_path=audio_path,
        language=language,
        initial_prompt=initial_prompt,
        timeout_seconds=timeout_seconds,
    )


async def _transcribe_asr(
    *,
    whisper_url: str,
    audio_path: Path,
    language: str | None,
    initial_prompt: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    endpoint = whisper_url.rstrip("/") + "/asr"
    params: dict[str, str] = {"output": "json", "word_timestamps": "true"}
    if language:
        params["language"] = language
    if initial_prompt:
        params["initial_prompt"] = initial_prompt

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        with audio_path.open("rb") as file_obj:
            files = {"audio_file": (audio_path.name, file_obj, "audio/wav")}
            response = await client.post(endpoint, params=params, files=files)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid whisper response type")
    return payload


async def _transcribe_cpp(
    *,
    whisper_url: str,
    audio_path: Path,
    language: str | None,
    initial_prompt: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    endpoint = whisper_url.rstrip("/") + "/inference"
    data: dict[str, str] = {"response_format": "verbose_json"}
    if language:
        data["language"] = language
    if initial_prompt:
        data["initial_prompt"] = initial_prompt

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        with audio_path.open("rb") as file_obj:
            files = {"file": (audio_path.name, file_obj, "audio/wav")}
            response = await client.post(endpoint, data=data, files=files)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid whisper.cpp response type")
    return payload


def normalize_whisper_output(
    payload: dict[str, Any],
    *,
    segment_offset_sec: float,
    backend: WhisperBackend = "asr",
) -> tuple[str, list[dict[str, Any]]]:
    if backend == "cpp":
        return _normalize_cpp(payload, segment_offset_sec=segment_offset_sec)
    return _normalize_asr(payload, segment_offset_sec=segment_offset_sec)


def _normalize_asr(
    payload: dict[str, Any],
    *,
    segment_offset_sec: float,
) -> tuple[str, list[dict[str, Any]]]:
    raw_segments = payload.get("segments", [])
    text = str(payload.get("text", "")).strip()
    words: list[dict[str, Any]] = []
    for seg in raw_segments if isinstance(raw_segments, list) else []:
        for word in seg.get("words", []) if isinstance(seg, dict) else []:
            words.append(
                {
                    "word": str(word.get("word", "")).strip(),
                    "start": float(word.get("start", 0.0)) + segment_offset_sec,
                    "end": float(word.get("end", 0.0)) + segment_offset_sec,
                    "confidence": word.get("probability"),
                }
            )
    return text, words


def _normalize_cpp(
    payload: dict[str, Any],
    *,
    segment_offset_sec: float,
) -> tuple[str, list[dict[str, Any]]]:
    raw_segments = payload.get("segments", [])
    text = str(payload.get("text", "")).strip()
    words: list[dict[str, Any]] = []
    for seg in raw_segments if isinstance(raw_segments, list) else []:
        if not isinstance(seg, dict):
            continue
        # whisper.cpp verbose_json: segments have a "words" array
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

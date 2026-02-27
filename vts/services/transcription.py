from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


async def transcribe_with_whisper(
    *,
    whisper_url: str,
    audio_path: Path,
    language: str | None,
    initial_prompt: str | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    endpoint = whisper_url.rstrip("/") + "/asr"
    params = {"output": "json", "word_timestamps": "true"}
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


def normalize_whisper_output(
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

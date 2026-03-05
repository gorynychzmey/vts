from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


async def transcribe_cpp(
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


async def detect_language_with_cpp(
    *,
    whisper_url: str,
    audio_path: Path,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Call whisper.cpp /inference with detect_language=true.

    Returns a dict with at least 'language' and optionally 'language_probs'
    or 'language_probabilities'. No transcription is performed.
    """
    endpoint = whisper_url.rstrip("/") + "/inference"
    data: dict[str, str] = {"detect_language": "true"}

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        with audio_path.open("rb") as file_obj:
            files = {"file": (audio_path.name, file_obj, "audio/wav")}
            response = await client.post(endpoint, data=data, files=files)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid whisper.cpp detect_language response type")
    return payload

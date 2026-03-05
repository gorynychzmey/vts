from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


async def transcribe_asr(
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

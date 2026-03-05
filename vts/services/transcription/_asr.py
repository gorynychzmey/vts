from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ._base import WhisperBackend


class AsrBackend(WhisperBackend):
    def __init__(self, whisper_url: str) -> None:
        self._url = whisper_url.rstrip("/")

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        initial_prompt: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        endpoint = self._url + "/asr"
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
            raise RuntimeError("Invalid whisper-asr response type")
        return payload

    async def detect_language(
        self,
        audio_path: Path,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        endpoint = self._url + "/detect-language"
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            with audio_path.open("rb") as file_obj:
                files = {"audio_file": (audio_path.name, file_obj, "audio/wav")}
                response = await client.post(endpoint, files=files)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid whisper-asr detect-language response type")
        return payload

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ._base import WhisperBackend


class CppBackend(WhisperBackend):
    def __init__(self, whisper_url: str) -> None:
        self._url = whisper_url.rstrip("/")

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        initial_prompt: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        endpoint = self._url + "/inference"
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

    async def detect_language(
        self,
        audio_path: Path,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        endpoint = self._url + "/inference"
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

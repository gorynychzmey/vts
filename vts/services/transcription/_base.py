from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx


class WhisperBackend(ABC):
    backend_name: str

    def __init__(self, whisper_url: str) -> None:
        self._url = whisper_url.rstrip("/")

    @abstractmethod
    async def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        initial_prompt: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def detect_language(
        self,
        audio_path: Path,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]: ...

    def normalize_output(self, payload: dict[str, Any]) -> str:
        return str(payload.get("text", "")).strip()

    async def _post_audio(
        self,
        endpoint: str,
        audio_path: Path,
        file_key: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        timeout_seconds: int,
        error_context: str,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            with audio_path.open("rb") as file_obj:
                files = {file_key: (audio_path.name, file_obj, "audio/wav")}
                response = await client.post(endpoint, params=params, data=data, files=files)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid {error_context} response type")
        return payload

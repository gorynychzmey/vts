from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import WhisperBackend


class AsrBackend(WhisperBackend):
    backend_name = "asr"

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        initial_prompt: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"output": "json", "word_timestamps": "true"}
        if language:
            params["language"] = language
        if initial_prompt:
            params["initial_prompt"] = initial_prompt
        return await self._post_audio(
            self._url + "/asr",
            audio_path,
            "audio_file",
            params=params,
            timeout_seconds=timeout_seconds,
            error_context="whisper-asr",
        )

    async def detect_language(
        self,
        audio_path: Path,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        return await self._post_audio(
            self._url + "/detect-language",
            audio_path,
            "audio_file",
            timeout_seconds=timeout_seconds,
            error_context="whisper-asr detect-language",
        )

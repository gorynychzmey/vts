from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import WhisperBackend


class CppBackend(WhisperBackend):
    backend_name = "cpp"

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None,
        initial_prompt: str | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"response_format": "verbose_json"}
        if language:
            data["language"] = language
        if initial_prompt:
            data["initial_prompt"] = initial_prompt
        return await self._post_audio(
            self._url + "/inference",
            audio_path,
            "file",
            data=data,
            timeout_seconds=timeout_seconds,
            error_context="whisper.cpp",
        )

    async def detect_language(
        self,
        audio_path: Path,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        return await self._post_audio(
            self._url + "/inference",
            audio_path,
            "file",
            data={"detect_language": "true"},
            timeout_seconds=timeout_seconds,
            error_context="whisper.cpp detect_language",
        )

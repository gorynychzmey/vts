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

    def normalize_output(
        self,
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

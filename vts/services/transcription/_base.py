from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class WhisperBackend(ABC):
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

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._base import WhisperBackend

_log = logging.getLogger(__name__)


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
        # whisper.cpp returns: {"language": "ru", "language_probs": {...}} or
        # {"language": "ru", "language_probabilities": {...}, "detected_language_probability": 0.99}
        raw = await self._post_audio(
            self._url + "/inference",
            audio_path,
            "file",
            data={"detect_language": "true"},
            timeout_seconds=timeout_seconds,
            error_context="whisper.cpp detect_language",
        )
        _log.debug("detect_language raw response: %s", raw)
        language = raw.get("language")
        probability: float | None = None
        for key in ("detected_language_probability", "language_probability", "language_confidence"):
            val = raw.get(key)
            if isinstance(val, (int, float)):
                probability = float(val)
                break
        if probability is None:
            for map_key in ("language_probs", "language_probabilities"):
                prob_map = raw.get(map_key)
                if isinstance(prob_map, dict) and language:
                    val = prob_map.get(language)
                    if isinstance(val, (int, float)):
                        probability = float(val)
                        break
        return {"language": language, "language_probability": probability}

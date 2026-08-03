from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import WhisperBackend


def normalize_detect_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise /detect-language output to {language, language_probability}.

    `raw.get("confidence") or raw.get("language_probability")` swallowed a
    legitimate 0.0, so DetectLanguageStep reported "probability missing"
    instead of "confidence too low" — precisely the case where the number
    matters most (vts-c58). Absence and zero are different answers.
    """
    confidence = raw.get("confidence")
    return {
        "language": raw.get("language_code") or raw.get("language"),
        "language_probability": (
            confidence if confidence is not None else raw.get("language_probability")
        ),
    }


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
        # ASR /detect-language returns: {"detected_language": "russian", "language_code": "ru", "confidence": 0.99}
        # Normalize to canonical: {"language": <code>, "language_probability": <float>}
        raw = await self._post_audio(
            self._url + "/detect-language",
            audio_path,
            "audio_file",
            timeout_seconds=timeout_seconds,
            error_context="whisper-asr detect-language",
        )
        return normalize_detect_payload(raw)

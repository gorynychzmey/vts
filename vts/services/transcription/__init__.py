from __future__ import annotations

from ._asr import AsrBackend
from ._base import WhisperBackend
from ._cpp import CppBackend


def create_whisper_backend(whisper_url: str, whisper_backend: str) -> WhisperBackend:
    if whisper_backend == "cpp":
        return CppBackend(whisper_url)
    return AsrBackend(whisper_url)


__all__ = [
    "AsrBackend",
    "CppBackend",
    "WhisperBackend",
    "create_whisper_backend",
]

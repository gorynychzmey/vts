from __future__ import annotations

from ._base import DiarizationBackend
from ._pyannote import PyannoteBackend


def create_diarization_backend(diarization_url: str, diarization_backend: str) -> DiarizationBackend:
    if diarization_backend == "pyannote":
        return PyannoteBackend(diarization_url)
    raise ValueError(f"Unknown diarization backend: {diarization_backend!r}. Expected 'pyannote'.")


__all__ = [
    "DiarizationBackend",
    "PyannoteBackend",
    "create_diarization_backend",
]

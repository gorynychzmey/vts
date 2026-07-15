from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx


class DiarizationBackend(ABC):
    backend_name: str

    def __init__(self, diarization_url: str) -> None:
        self._url = diarization_url.rstrip("/")

    @abstractmethod
    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]: ...

    def normalize_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Canonical shape: {"segments": [...], "embeddings": {...}, "num_speakers": int}.

        Malformed segments are dropped rather than raising: a partial
        diarization still beats failing a whole task over one bad span.
        """
        segments: list[dict[str, Any]] = []
        for segment in payload.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            if segment.get("start") is None or segment.get("end") is None:
                continue
            if not segment.get("speaker"):
                continue
            segments.append(
                {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "speaker": str(segment["speaker"]),
                }
            )

        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, dict):
            embeddings = {}

        num_speakers = payload.get("num_speakers")
        if not isinstance(num_speakers, int):
            num_speakers = len({segment["speaker"] for segment in segments})

        return {"segments": segments, "embeddings": embeddings, "num_speakers": num_speakers}

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

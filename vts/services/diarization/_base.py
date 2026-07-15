from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)


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
        raw_segments = payload.get("segments")
        # The sidecar is a separate process: a wrong type here (e.g. an int
        # or a string instead of a list) must not be iterated.
        if not isinstance(raw_segments, list):
            raw_segments = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("start") is None or segment.get("end") is None:
                continue
            if not segment.get("speaker"):
                continue
            try:
                coerced = {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "speaker": str(segment["speaker"]),
                }
            except (TypeError, ValueError):
                # One unparsable field (e.g. non-numeric start/end) drops
                # only this segment, keeping the partial-diarization promise.
                continue
            segments.append(coerced)

        # Dropping is silent by design, which makes a systematically broken
        # sidecar look like a quiet monologue rather than a failure. Say so in
        # the log, so it is visible on day one instead of after someone notices
        # transcripts stopped carrying speakers.
        if raw_segments and not segments:
            _log.warning(
                "diarization response had %d segment(s) but none survived normalization: %r",
                len(raw_segments),
                payload,
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

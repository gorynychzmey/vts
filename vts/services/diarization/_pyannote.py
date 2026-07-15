from __future__ import annotations

from pathlib import Path
from typing import Any

from ._base import DiarizationBackend


class PyannoteBackend(DiarizationBackend):
    backend_name = "pyannote"

    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        payload = await self._post_audio(
            self._url + "/diarize",
            audio_path,
            "file",
            timeout_seconds=timeout_seconds,
            error_context="pyannote",
        )
        return self.normalize_output(payload)

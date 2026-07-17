from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ._base import DiarizationBackend


class PyannoteBackend(DiarizationBackend):
    backend_name = "pyannote"

    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
        *,
        job_id: str | None = None,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        # Callers pass their task id, which is what makes a restart able to
        # re-attach. A generated one still works, it just cannot survive us.
        payload = await self._run_job(
            audio_path,
            job_id or str(uuid.uuid4()),
            timeout_seconds,
            on_progress,
        )
        return self.normalize_output(payload)

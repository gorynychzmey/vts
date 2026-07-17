from __future__ import annotations

import json
import time
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# Long enough that a slow first read does not look like a hung sidecar, short
# enough that a genuinely dead one is noticed. The work itself is not on this
# clock — that is the whole point of the job API.
_CONTROL_TIMEOUT_SECONDS = 30.0

# Uploading a 90 MB meeting over a loopback is quick, but the sidecar may be on
# another host; scale with the file rather than guess a constant.
_UPLOAD_SECONDS_PER_MB = 2.0
_UPLOAD_MIN_SECONDS = 60.0

_monotonic = time.monotonic


def timeout_for_upload(audio_path: Path) -> float:
    try:
        megabytes = audio_path.stat().st_size / (1024 * 1024)
    except OSError:
        return _UPLOAD_MIN_SECONDS
    return max(_UPLOAD_MIN_SECONDS, megabytes * _UPLOAD_SECONDS_PER_MB)


class DiarizationBackend(ABC):
    backend_name: str

    def __init__(self, diarization_url: str) -> None:
        self._url = diarization_url.rstrip("/")

    def _client(self, timeout: float) -> httpx.AsyncClient:
        # A seam for tests to inject a MockTransport; production just gets a
        # plain client bound to the control-plane timeout.
        return httpx.AsyncClient(timeout=timeout)

    async def cancel(self, job_id: str) -> None:
        """Tell the sidecar to drop a job whose owner is gone.

        Best-effort: the caller is already abandoning this work, so a sidecar
        that cannot be reached must not turn a cancellation into a failure. The
        job's own idle TTL is the backstop.
        """
        try:
            async with self._client(_CONTROL_TIMEOUT_SECONDS) as client:
                await client.delete(f"{self._url}/jobs/{job_id}")
        except httpx.HTTPError:
            _log.warning("could not cancel diarization job %s", job_id, exc_info=True)

    async def list_jobs(self) -> list[str]:
        """Job ids the sidecar currently knows about.

        Best-effort, like cancel: a sidecar that cannot be reached (down, or an
        old build without the endpoint) yields an empty list rather than an
        error, so startup reconciliation degrades to "nothing to reconcile"
        instead of failing the worker's boot.
        """
        try:
            async with self._client(_CONTROL_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._url}/jobs")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            _log.warning("could not list diarization jobs for reconciliation", exc_info=True)
            return []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        return [str(j["job_id"]) for j in jobs if isinstance(j, dict) and "job_id" in j]

    @abstractmethod
    async def diarize(
        self,
        audio_path: Path,
        timeout_seconds: int = 1800,
        *,
        job_id: str | None = None,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
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

    async def _run_job(
        self,
        audio_path: Path,
        job_id: str,
        timeout_seconds: int,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None,
    ) -> dict[str, Any]:
        """Start (or re-attach to) a diarization job and collect its result.

        The three calls exist so the work outlives the connection: starting
        returns at once, watching is disposable, and the result is fetched by id
        — so a worker restart mid-run costs a reconnect instead of 25 minutes.
        """
        async with self._client(_CONTROL_TIMEOUT_SECONDS) as client:
            state = await self._start_job(client, audio_path, job_id)
            if state.get("retried_after_error"):
                # The sidecar handed us the previous failure exactly once, and
                # has already started over. Record it: nothing else will.
                _log.warning(
                    "diarization job %s restarted after error: %s",
                    job_id, state["retried_after_error"],
                )
            if state.get("state") != "done":
                await self._watch_job(client, job_id, timeout_seconds, on_progress)
            response = await client.get(f"{self._url}/jobs/{job_id}/result")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid pyannote response type")
        return payload

    async def _start_job(
        self, client: httpx.AsyncClient, audio_path: Path, job_id: str
    ) -> dict[str, Any]:
        with audio_path.open("rb") as file_obj:
            response = await client.post(
                f"{self._url}/diarize",
                data={"job_id": job_id},
                files={"file": (audio_path.name, file_obj, "audio/wav")},
                timeout=timeout_for_upload(audio_path),
            )
        response.raise_for_status()
        state = response.json()
        if not isinstance(state, dict):
            raise RuntimeError("Invalid pyannote job response")
        return state

    async def _watch_job(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        timeout_seconds: int,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None,
    ) -> None:
        """Follow the job's progress stream until it ends.

        A dropped stream is not a failed job: reconnect and keep watching. Only
        the sidecar saying "failed", or the overall deadline, ends this badly.
        """
        deadline = _monotonic() + timeout_seconds
        while _monotonic() < deadline:
            try:
                async for event in self._read_events(client, job_id, deadline):
                    state = event.get("state")
                    if state == "failed":
                        raise RuntimeError(f"diarization failed: {event.get('error')}")
                    if state in ("done", "gone"):
                        return
                    if on_progress is not None:
                        await on_progress(
                            str(event.get("step", "")),
                            int(event.get("completed", 0) or 0),
                            int(event.get("total", 0) or 0),
                        )
                return  # stream ended cleanly without a terminal event
            except (httpx.TransportError, httpx.RemoteProtocolError):
                # Watching is disposable; the work is not. Try to re-attach.
                _log.info("diarization progress stream for %s dropped; reconnecting", job_id)
        raise TimeoutError(f"diarization job {job_id} exceeded {timeout_seconds}s")

    async def _read_events(
        self, client: httpx.AsyncClient, job_id: str, deadline: float
    ) -> Any:
        remaining = max(deadline - _monotonic(), 1.0)
        async with client.stream(
            "GET", f"{self._url}/jobs/{job_id}/events", timeout=remaining
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        yield json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

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

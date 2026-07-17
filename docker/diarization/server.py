"""Diarization sidecar: pyannote behind one HTTP endpoint.

Lives in its own container so the main app stays PyTorch-free — VTS is an
orchestrator, and every ML model it uses runs behind HTTP.

The wire contract is {"segments", "embeddings", "num_speakers"}; the client and
its tests depend on that shape, not on pyannote's internals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pyannote.audio import Pipeline

# How often the SSE stream re-reads job progress. pyannote reports per embedding
# batch (~1.4s apart on a real meeting), so polling faster only burns wakeups.
_PROGRESS_POLL_SECONDS = 1.0
_REAPER_INTERVAL_SECONDS = 30

_log = logging.getLogger("diarization")

# Without this the sidecar's own logs vanish: uvicorn configures its loggers and
# leaves the root at WARNING, so every _log.info here (which weights loaded,
# which precision was chosen, how many speakers came back) was silently dropped.
# The precision line in particular has to reach a foreign deployment's logs —
# that is the whole point of detecting hardware instead of assuming it.
logging.basicConfig(
    level=os.environ.get("DIAR_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI()
_pipeline: Pipeline | None = None


def _has_bf16_kernels() -> bool:
    """Whether this CPU has hardware bf16, not just the ability to fake it.

    autocast never refuses: without avx512_bf16 it emulates, and emulation is
    slower than the fp32 it replaces. That is not theory — ONNX int8 measured
    0.22x on this same box for exactly that reason. So the flag is the gate.
    """
    probe = getattr(torch.cpu, "_is_avx512_bf16_supported", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a probe that raises tells us nothing
            _log.debug("bf16 probe raised; falling back to /proc/cpuinfo", exc_info=True)
    try:
        return "avx512_bf16" in Path("/proc/cpuinfo").read_text()
    except OSError:
        return False


def _resolve_precision() -> str:
    """Pick the inference precision: DIAR_PRECISION=auto|bf16|fp32.

    `auto` turns bf16 on only where the hardware kernels are present. Anything
    beyond that is guessing on someone else's CPU, and guessing wrong here costs
    more than doing nothing.
    """
    requested = os.environ.get("DIAR_PRECISION", "auto").strip().lower()
    if requested not in {"auto", "bf16", "fp32"}:
        _log.warning("ignoring unknown DIAR_PRECISION=%r; using auto", requested)
        requested = "auto"

    has_bf16 = _has_bf16_kernels()
    if requested == "fp32":
        _log.info("precision=fp32 (requested)")
        return "fp32"
    if requested == "bf16":
        if not has_bf16:
            # Honour the override, but say plainly that it will emulate.
            _log.warning("precision=bf16 forced, but this CPU has no avx512_bf16: expect a SLOWDOWN")
        else:
            _log.info("precision=bf16 (requested)")
        return "bf16"
    if has_bf16:
        _log.info("precision=bf16 (auto: avx512_bf16 present)")
        return "bf16"
    _log.info("precision=fp32 (auto: no avx512_bf16 on this CPU)")
    return "fp32"


class _Bf16Resnet(torch.nn.Module):
    """Runs the embedder's ResNet in bf16 and hands fp32 back.

    Scoped to the resnet on purpose. The embedder is ~98% of diarization wall
    time (segmentation is ~1.9%), so this is the whole prize; and a
    pipeline-wide autocast breaks outright — pyannote passes the segmentation
    head's output to .numpy(), which has no bf16 dtype.
    """

    def __init__(self, inner: torch.nn.Module) -> None:
        super().__init__()
        self.inner = inner

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        with torch.autocast("cpu", dtype=torch.bfloat16):
            out = self.inner(*args, **kwargs)
        if isinstance(out, tuple):
            return tuple(o.float() if torch.is_tensor(o) else o for o in out)
        return out.float() if torch.is_tensor(out) else out


def _apply_precision(pipe: Pipeline) -> None:
    """Swap the embedder's resnet for a bf16 (and optionally compiled) one."""
    if _resolve_precision() != "bf16":
        return
    try:
        embedding = pipe._embedding.model_
        resnet = embedding.resnet
    except AttributeError:
        # A pyannote upgrade could rename this path; speed is not worth a crash.
        _log.warning("embedder resnet not found; leaving precision at fp32", exc_info=True)
        return

    if os.environ.get("DIAR_COMPILE", "1").strip().lower() not in {"0", "false", "no"}:
        try:
            resnet = torch.compile(resnet)
            _log.info("torch.compile enabled for the embedder")
        except Exception:  # noqa: BLE001 - compile needs a C++ toolchain; bf16 alone still wins
            _log.warning("torch.compile unavailable; using bf16 without it", exc_info=True)

    embedding.resnet = _Bf16Resnet(resnet)


def pipeline() -> Pipeline:
    """The diarization pipeline, loaded once from the vendored weights.

    Loading is lazy so the container answers /health while the first model load
    is still in flight, and so an import-time failure cannot mask the reason.
    """
    global _pipeline
    if _pipeline is None:
        model_dir = os.environ.get("MODEL_DIR", "/models")
        _log.info("loading pipeline from %s", model_dir)
        _pipeline = Pipeline.from_pretrained(Path(model_dir) / "config.yaml")
        if _pipeline is None:
            raise RuntimeError(f"pyannote returned no pipeline for {model_dir}")
        _pipeline.to(torch.device(os.environ.get("TORCH_DEVICE", "cpu")))
        _apply_min_duration_off(_pipeline)
        _apply_precision(_pipeline)
    return _pipeline


def _apply_min_duration_off(pipe: Pipeline) -> None:
    """Override segmentation.min_duration_off from the environment.

    It fills inactive regions shorter than N seconds, merging a speaker's own
    breathing pauses into one segment. Calibrated to 0.5 on a real 4-speaker
    meeting: it halves segment fragmentation (785 -> 413) while barely touching
    cross-speaker boundaries (126 -> 120), which by design it never fills. The
    model config ships 0.0, so this is opt-in and tunable without a rebuild.
    """
    raw = os.environ.get("DIAR_MIN_DURATION_OFF")
    if raw is None:
        return
    try:
        value = float(raw)
    except ValueError:
        _log.warning("ignoring non-numeric DIAR_MIN_DURATION_OFF=%r", raw)
        return
    params = pipe.parameters(instantiated=True)
    if "segmentation" not in params:
        _log.warning("pipeline has no segmentation params; DIAR_MIN_DURATION_OFF ignored")
        return
    params["segmentation"]["min_duration_off"] = value
    pipe.instantiate(params)
    _log.info("segmentation.min_duration_off set to %.2f", value)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _extract_embeddings(raw: Any, labels: list[str]) -> dict[str, list[float]]:
    """Per-speaker embedding vectors, keyed by the diarization's own labels.

    pyannote returns embeddings positionally, aligned with `labels`; anything
    unexpected degrades to no embeddings rather than failing the request —
    vts-5xz does not read them, and vts-80i can recompute.
    """
    if raw is None:
        return {}
    embeddings: dict[str, list[float]] = {}
    for index, label in enumerate(labels):
        try:
            vector = raw[index]
        except (IndexError, KeyError, TypeError):
            continue
        try:
            embeddings[str(label)] = [float(value) for value in vector]
        except (TypeError, ValueError):
            continue
    return embeddings


def _shape_output(output: Any) -> dict[str, Any]:
    """pyannote's DiarizeOutput -> the wire contract."""
    # pyannote 4.x returns a DiarizeOutput carrying two Annotations and the
    # embeddings; no `return_embeddings` flag is involved.
    #
    # `exclusive_speaker_diarization` is the one to use: the consumer attributes
    # each word to exactly one speaker, so overlapping turns would force an
    # arbitrary pick anyway. The exclusive variant makes that choice upstream,
    # where the model has the acoustic evidence to make it.
    diarization = output.exclusive_speaker_diarization

    segments = [
        {"start": float(turn.start), "end": float(turn.end), "speaker": str(speaker)}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
    labels = [str(label) for label in diarization.labels()]
    # Embeddings are positional, aligned with the labels of the non-exclusive
    # annotation — that is the annotation clustering produced them from.
    embeddings = _extract_embeddings(
        output.speaker_embeddings,
        [str(label) for label in output.speaker_diarization.labels()],
    )

    _log.info("diarized: speakers=%d segments=%d", len(labels), len(segments))
    return {"segments": segments, "embeddings": embeddings, "num_speakers": len(labels)}


class _Cancelled(Exception):
    """Raised out of the pyannote hook to unwind a job the caller dropped."""


class _Job:
    """One diarization, addressable by the caller's own id.

    The id comes from the client, not from here: a server-generated one has a
    window where the job exists but its owner never learned the id — die there
    and the job is orphaned with nothing to name it. The worker knows its
    task_id before it asks, so it can always come back and ask about it.
    """

    __slots__ = ("job_id", "status", "progress", "result", "error", "cancel", "touched_at", "task")

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.status = "running"
        self.progress: dict[str, Any] = {"step": "starting", "completed": 0, "total": 0}
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.cancel = False
        self.touched_at = time.monotonic()
        self.task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        self.touched_at = time.monotonic()


_jobs: dict[str, _Job] = {}


def _job_ttl(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _log.warning("ignoring non-numeric %s=%r; using %ds", name, raw, default)
        return default


def _run_diarization(job: _Job, audio_path: Path) -> dict[str, Any]:
    """Blocking pyannote call, reporting progress and honouring cancellation.

    Runs in a worker thread: pyannote is synchronous and would otherwise block
    the event loop, leaving /events and /jobs unable to answer for the whole run.
    """

    def hook(step: str, artefact: Any = None, file: Any = None, total: int | None = None,
             completed: int | None = None) -> None:
        if job.cancel:
            raise _Cancelled
        job.touch()
        # `embeddings` reports per batch and is ~98% of the wall time; the other
        # steps fire once each, so they read as instant. Reporting all of them
        # keeps the UI honest about which phase is slow.
        job.progress = {
            "step": step,
            "completed": int(completed) if completed is not None else 0,
            "total": int(total) if total is not None else 0,
        }

    return _shape_output(pipeline()(str(audio_path), hook=hook))


async def _diarize_job(job: _Job, audio_path: Path) -> None:
    try:
        job.result = await asyncio.to_thread(_run_diarization, job, audio_path)
        job.status = "done"
    except _Cancelled:
        job.status = "cancelled"
        _log.info("job %s cancelled", job.job_id)
        _jobs.pop(job.job_id, None)
    except Exception as error:  # noqa: BLE001 - the cause goes back to the caller
        _log.exception("job %s failed", job.job_id)
        job.status = "failed"
        job.error = str(error)
    finally:
        job.touch()
        audio_path.unlink(missing_ok=True)


@app.post("/diarize")
async def diarize(job_id: str = Form(...), file: UploadFile = File(...)) -> dict[str, Any]:
    """Start a diarization, or re-attach to the one this id already names.

    Returning rather than blocking is what lets the job outlive the worker: a
    restart mid-run reconnects by id instead of paying for the whole thing twice.
    """
    existing = _jobs.get(job_id)
    if existing is not None:
        existing.touch()
        if existing.status == "running":
            return {"job_id": job_id, "state": "running"}
        if existing.status == "done":
            # The worker restarted and came back after we finished. Point it at
            # the result rather than at a stream that will never say anything.
            return {"job_id": job_id, "state": "done"}
        if existing.status == "failed":
            # Hand the reason over exactly once, then forget it and start
            # afresh: a retained failure would answer the next retry with
            # "running" on a dead job and hang it on a silent stream.
            reason = existing.error
            _jobs.pop(job_id, None)
            _log.info("job %s restarting after error: %s", job_id, reason)
            job = await _start_job(job_id, file)
            return {"job_id": job.job_id, "state": "running", "retried_after_error": reason}

    job = await _start_job(job_id, file)
    return {"job_id": job.job_id, "state": "running"}


async def _start_job(job_id: str, file: UploadFile) -> _Job:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(await file.read())
        audio_path = Path(handle.name)
    job = _Job(job_id)
    _jobs[job_id] = job
    job.task = asyncio.create_task(_diarize_job(job, audio_path))
    _log.info("job %s started", job_id)
    return job


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Progress as SSE. Losing this stream costs the watcher, not the work."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")

    async def stream() -> AsyncIterator[str]:
        last: str | None = None
        while True:
            job = _jobs.get(job_id)
            if job is None:  # cancelled or evicted out from under us
                yield f"data: {json.dumps({'state': 'gone'})}\n\n"
                return
            job.touch()
            payload = {"state": job.status, **job.progress}
            if job.status in ("done", "failed"):
                if job.status == "failed":
                    payload["error"] = job.error
                yield f"data: {json.dumps(payload)}\n\n"
                return
            serialised = json.dumps(payload)
            if serialised != last:  # only speak when something changed
                yield f"data: {serialised}\n\n"
                last = serialised
            await asyncio.sleep(_PROGRESS_POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/jobs/{job_id}/result")
async def job_result(job_id: str) -> dict[str, Any]:
    """Collect the result, which also disposes of the job.

    Separate from the stream on purpose: a disconnect on the last event would
    otherwise lose a result that is already computed and sitting right here.
    """
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    if job.status == "running":
        raise HTTPException(status_code=409, detail="job still running")
    if job.status == "failed":
        error = job.error
        _jobs.pop(job_id, None)
        raise HTTPException(status_code=500, detail=f"diarization failed: {error}")
    result = job.result or {}
    _jobs.pop(job_id, None)
    return result


@app.delete("/jobs/{job_id}")
async def job_cancel(job_id: str) -> dict[str, str]:
    """Stop a job whose task is gone. The hook unwinds pyannote's own loop."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    job.cancel = True
    _log.info("job %s cancellation requested", job_id)
    return {"job_id": job_id, "state": "cancelling"}


@app.get("/jobs")
async def job_list() -> dict[str, Any]:
    """Every job this process knows about, so a restarted worker can reconcile."""
    return {
        "jobs": [
            {"job_id": j.job_id, "state": j.status, **j.progress}
            for j in _jobs.values()
        ]
    }


async def _reap_jobs() -> None:
    """Evict jobs nobody came back for.

    Two clocks, because the two cases fail differently. A running job whose
    owner stopped asking is burning 8 threads for a result no one will collect.
    A finished one is only holding memory, so it can wait much longer for a
    worker that is slow to return.

    Both measure from the last touch, never from the start: a 25-minute job
    timed from its start would evict itself mid-run.
    """
    run_ttl = _job_ttl("DIAR_JOB_RUN_TTL_SECONDS", 900)
    keep_ttl = _job_ttl("DIAR_JOB_RESULT_TTL_SECONDS", 3600)
    while True:
        await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
        now = time.monotonic()
        for job_id, job in list(_jobs.items()):
            idle = now - job.touched_at
            if job.status == "running" and idle > run_ttl:
                _log.warning("job %s: no one asked for %.0fs, cancelling", job_id, idle)
                job.cancel = True
            elif job.status in ("done", "failed") and idle > keep_ttl:
                _log.info("job %s: result uncollected for %.0fs, dropping", job_id, idle)
                _jobs.pop(job_id, None)


@app.on_event("startup")
async def _start_reaper() -> None:
    app.state.reaper = asyncio.create_task(_reap_jobs())


@app.on_event("shutdown")
async def _stop_reaper() -> None:
    reaper = getattr(app.state, "reaper", None)
    if reaper is not None:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper

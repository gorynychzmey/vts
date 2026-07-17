"""Diarization sidecar: pyannote behind one HTTP endpoint.

Lives in its own container so the main app stays PyTorch-free — VTS is an
orchestrator, and every ML model it uses runs behind HTTP.

The wire contract is {"segments", "embeddings", "num_speakers"}; the client and
its tests depend on that shape, not on pyannote's internals.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from pyannote.audio import Pipeline

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


@app.post("/diarize")
async def diarize(file: UploadFile = File(...)) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(await file.read())
        audio_path = Path(handle.name)
    try:
        output = pipeline()(str(audio_path))
    except Exception as error:  # noqa: BLE001 - surface the cause to the caller
        _log.exception("diarization failed")
        raise HTTPException(status_code=500, detail=f"diarization failed: {error}") from error
    finally:
        audio_path.unlink(missing_ok=True)

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

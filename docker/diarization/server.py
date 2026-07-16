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

app = FastAPI()
_pipeline: Pipeline | None = None


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
    return _pipeline


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

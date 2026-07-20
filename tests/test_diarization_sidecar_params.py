"""Unit tests for the diarization sidecar's env-driven param overrides.

The sidecar (docker/diarization/server.py) imports torch/pyannote lazily, so the
module itself imports fine here and the pure param-application helpers can be
tested against a fake pipeline that mimics pyannote's
parameters(instantiated=True) / instantiate() contract. This covers the
clustering (Fa/Fb/threshold) overrides added for short-interjection separation;
the model forward pass is out of scope and validated live.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "diarization"))
import server  # noqa: E402


class FakePipeline:
    """Mimics the slice of a pyannote Pipeline the helpers touch."""

    def __init__(self, params: dict) -> None:
        self._params = params
        self.instantiated_with: dict | None = None

    def parameters(self, instantiated: bool = False) -> dict:
        # Return the live dict so in-place updates by the helper are visible,
        # matching how the real pipeline hands back its instantiated params.
        return self._params

    def instantiate(self, params: dict) -> None:
        self.instantiated_with = params


def test_clustering_params_unset_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DIAR_CLUSTERING_FA", "DIAR_CLUSTERING_FB", "DIAR_CLUSTERING_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    pipe = FakePipeline({"clustering": {"Fa": 0.07, "Fb": 0.8, "threshold": 0.6}})
    server._apply_clustering_params(pipe)
    assert pipe.instantiated_with is None  # never re-instantiated when nothing set


def test_clustering_fa_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIAR_CLUSTERING_FA", "0.15")
    monkeypatch.delenv("DIAR_CLUSTERING_FB", raising=False)
    monkeypatch.delenv("DIAR_CLUSTERING_THRESHOLD", raising=False)
    pipe = FakePipeline({"clustering": {"Fa": 0.07, "Fb": 0.8, "threshold": 0.6}})
    server._apply_clustering_params(pipe)
    assert pipe.instantiated_with is not None
    assert pipe.instantiated_with["clustering"]["Fa"] == pytest.approx(0.15)
    # Untouched knobs keep their model defaults.
    assert pipe.instantiated_with["clustering"]["Fb"] == pytest.approx(0.8)
    assert pipe.instantiated_with["clustering"]["threshold"] == pytest.approx(0.6)


def test_clustering_multiple_overrides_applied_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIAR_CLUSTERING_FA", "0.2")
    monkeypatch.setenv("DIAR_CLUSTERING_THRESHOLD", "0.5")
    monkeypatch.delenv("DIAR_CLUSTERING_FB", raising=False)
    pipe = FakePipeline({"clustering": {"Fa": 0.07, "Fb": 0.8, "threshold": 0.6}})
    server._apply_clustering_params(pipe)
    assert pipe.instantiated_with["clustering"]["Fa"] == pytest.approx(0.2)
    assert pipe.instantiated_with["clustering"]["threshold"] == pytest.approx(0.5)


def test_clustering_non_numeric_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIAR_CLUSTERING_FA", "not-a-number")
    monkeypatch.delenv("DIAR_CLUSTERING_FB", raising=False)
    monkeypatch.delenv("DIAR_CLUSTERING_THRESHOLD", raising=False)
    pipe = FakePipeline({"clustering": {"Fa": 0.07, "Fb": 0.8, "threshold": 0.6}})
    server._apply_clustering_params(pipe)
    # A single bad value is the only override -> nothing valid -> no-op.
    assert pipe.instantiated_with is None


def test_clustering_missing_section_warns_no_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIAR_CLUSTERING_FA", "0.15")
    monkeypatch.delenv("DIAR_CLUSTERING_FB", raising=False)
    monkeypatch.delenv("DIAR_CLUSTERING_THRESHOLD", raising=False)
    pipe = FakePipeline({"segmentation": {"min_duration_off": 0.0}})  # no clustering
    server._apply_clustering_params(pipe)
    assert pipe.instantiated_with is None  # bailed out rather than KeyError

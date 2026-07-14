from __future__ import annotations

from vts.pipeline.steps.base import Step

STEP_REGISTRY: dict[str, Step] = {}


def resolve_step(step_name: str) -> Step:
    raise KeyError(step_name)

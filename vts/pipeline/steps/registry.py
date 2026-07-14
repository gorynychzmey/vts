from __future__ import annotations

from vts.pipeline.steps.base import Step
from vts.pipeline.steps.media import (
    DownloadStep,
    ExtractAudioStep,
    SegmentAudioStep,
    TrimInitialSilenceStep,
)

STEP_REGISTRY: dict[str, Step] = {
    DownloadStep.name: DownloadStep(),
    ExtractAudioStep.name: ExtractAudioStep(),
    TrimInitialSilenceStep.name: TrimInitialSilenceStep(),
    SegmentAudioStep.name: SegmentAudioStep(),
}


def resolve_step(step_name: str) -> Step:
    return STEP_REGISTRY[step_name]

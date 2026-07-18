from __future__ import annotations

from vts.pipeline.steps.base import Step
from vts.pipeline.steps.diarization import DiarizeStep
from vts.pipeline.steps.media import (
    DownloadStep,
    ExtractAudioStep,
    SegmentAudioStep,
    TrimInitialSilenceStep,
)
from vts.pipeline.steps.speaker_match import MatchSpeakersStep
from vts.pipeline.steps.summarization import (
    FinalizePromptStep,
    PackWindowNotesStep,
    PrepareLlamaModelStep,
    PrepareSummaryChunksStep,
    SummarizeWindowsStep,
)
from vts.pipeline.steps.transcription import (
    DetectLanguageStep,
    MergeTranscriptStep,
    TranscribeSegmentsStep,
)
from vts.services.prompt_registry import parse_ref

STEP_REGISTRY: dict[str, Step] = {
    DownloadStep.name: DownloadStep(),
    ExtractAudioStep.name: ExtractAudioStep(),
    TrimInitialSilenceStep.name: TrimInitialSilenceStep(),
    SegmentAudioStep.name: SegmentAudioStep(),
    DetectLanguageStep.name: DetectLanguageStep(),
    TranscribeSegmentsStep.name: TranscribeSegmentsStep(),
    DiarizeStep.name: DiarizeStep(),
    MergeTranscriptStep.name: MergeTranscriptStep(),
    PrepareLlamaModelStep.name: PrepareLlamaModelStep(),
    MatchSpeakersStep.name: MatchSpeakersStep(),
    PrepareSummaryChunksStep.name: PrepareSummaryChunksStep(),
    SummarizeWindowsStep.name: SummarizeWindowsStep(),
    PackWindowNotesStep.name: PackWindowNotesStep(),
}


def resolve_step(step_name: str) -> Step:
    # Finalize steps are generated per selected prompt under a dynamic name that
    # is not a registry key: `summarize_final` is the canonical system summary,
    # and `finalize:<source>:<id>` selects an arbitrary system/user prompt.
    if step_name == "summarize_final":
        return FinalizePromptStep(source="system", id="summary")
    if step_name.startswith("finalize:"):
        source, id = parse_ref(step_name.split(":", 1)[1])
        return FinalizePromptStep(source=source, id=id)
    return STEP_REGISTRY[step_name]

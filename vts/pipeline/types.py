from __future__ import annotations

from typing import Final


DAG_STEPS: Final[list[str]] = [
    "download",
    "extract_audio",
    "segment_audio",
    "transcribe_segments",
    "merge_transcript",
    "prepare_llama_model",
    "summarize_windows",
    "summarize_final",
]

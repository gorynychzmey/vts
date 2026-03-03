from __future__ import annotations

from typing import Final


DAG_STEPS: Final[list[str]] = [
    "download",
    "extract_audio",
    "trim_initial_silence",
    "segment_audio",
    "detect_language",
    "transcribe_segments",
    "merge_transcript",
    "prepare_llama_model",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
    "summarize_final",
]

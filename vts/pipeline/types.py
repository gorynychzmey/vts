from __future__ import annotations

from typing import Final

from vts.services.prompt_registry import ref_key
from vts.services.task_progress import selected_prompt_refs

DAG_HEAD: Final[list[str]] = [
    "download",
    "extract_audio",
    "trim_initial_silence",
    "segment_audio",
    "detect_language",
    "transcribe_segments",
    # Needs transcription's chunks done; merge_transcript consumes the speaker
    # artifact this step writes, so it must run before that.
    "diarize",
    "merge_transcript",
    "prepare_llama_model",
    # Matches diarized speaker clusters against the registry; pauses into
    # awaiting_input when a speaker doesn't auto-resolve (unless opted out).
    "match_speakers",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
]

# Back-compat: the full static list (summary-only pipeline). Kept for any
# consumer that imported DAG_STEPS expecting the legacy shape.
DAG_STEPS: Final[list[str]] = DAG_HEAD + ["summarize_final"]

def finalize_step_name(source: str, id: str) -> str:
    if source == "system" and id == "summary":
        return "summarize_final"
    return f"finalize:{ref_key(source, id)}"


def build_dag_steps(options: dict) -> list[str]:
    tail = [finalize_step_name(r["source"], r["id"]) for r in selected_prompt_refs(options)]
    return DAG_HEAD + tail

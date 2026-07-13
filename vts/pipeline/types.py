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
    "merge_transcript",
    "prepare_llama_model",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
]

# Back-compat: the full static list (summary-only pipeline). Kept for any
# consumer that imported DAG_STEPS expecting the legacy shape.
DAG_STEPS: Final[list[str]] = DAG_HEAD + ["summarize_final"]

# Steps whose whole body runs under a lane slot (acquired in _run_step).
# GPU steps are NOT listed: they acquire the gpu lane per GPU call inside
# their method bodies (former heavy-slot sites).
STEP_LANES: Final[dict[str, str]] = {
    "download": "network",
    "extract_audio": "ffmpeg",
    "trim_initial_silence": "ffmpeg",
    "segment_audio": "ffmpeg",
}


def lane_for_step(name: str) -> str | None:
    return STEP_LANES.get(name)


def finalize_step_name(source: str, id: str) -> str:
    if source == "system" and id == "summary":
        return "summarize_final"
    return f"finalize:{ref_key(source, id)}"


def build_dag_steps(options: dict) -> list[str]:
    tail = [finalize_step_name(r["source"], r["id"]) for r in selected_prompt_refs(options)]
    return DAG_HEAD + tail

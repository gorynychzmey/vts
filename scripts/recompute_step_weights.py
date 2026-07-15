# scripts/recompute_step_weights.py
"""One-off recompute of progress-bar step weights from completed runs (vts-b6t).

Queries completed steps of completed tasks, computes median per-step durations
(summarize_windows normalized per window), and prints a ready-to-paste block for
vts/static/app.js. Does NOT edit app.js — paste the numbers by hand.

Run:  python -m scripts.recompute_step_weights
Needs the same DB config as the app (database_url / VTS_* env).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from vts.db.models import Step, StepStatus, Task, TaskStatus
from vts.db.session import get_db_session_factory
from vts.metrics.step_weights import StepDuration, aggregate_step_weights, median
from vts.services.task_progress import summary_progress_for_task

# Fixed-order steps mirrored from app.js STEP_WEIGHT_SECONDS for stable output.
# A step missing here is measured but never printed, so whoever runs this script
# to obtain a new weight silently gets nothing back for it. Keep in sync with
# DAG_HEAD (vts/pipeline/types.py).
_PRINT_ORDER = [
    "download",
    "extract_audio",
    "trim_initial_silence",
    "segment_audio",
    "detect_language",
    "transcribe_segments",
    "diarize",
    "merge_transcript",
    "prepare_llama_model",
    "prepare_summary_chunks",
    "summarize_windows",
    "pack_window_notes",
]


async def _collect_rows() -> tuple[list[StepDuration], list[int]]:
    factory = get_db_session_factory()
    rows: list[StepDuration] = []
    window_totals: list[int] = []
    async with factory() as session:
        result = await session.execute(
            select(Task).where(Task.status == TaskStatus.completed)
        )
        tasks = result.scalars().all()
        for task in tasks:
            _current, total = summary_progress_for_task(task)
            window_total = total if total >= 1 else None
            if window_total is not None:
                window_totals.append(window_total)
            steps_result = await session.execute(
                select(Step).where(
                    Step.task_id == task.id, Step.status == StepStatus.completed
                )
            )
            for step in steps_result.scalars().all():
                if step.started_at is None or step.finished_at is None:
                    continue
                duration = (step.finished_at - step.started_at).total_seconds()
                if duration < 0:
                    continue
                rows.append(StepDuration(step.name, duration, window_total))
    return rows, window_totals


def _count(rows: list[StepDuration], name: str) -> int:
    if name == "summarize_windows":
        return sum(1 for r in rows if r.name == name and isinstance(r.window_total, int) and r.window_total >= 1)
    return sum(1 for r in rows if r.name == name)


def _final_summary_fallback(rows: list[StepDuration]) -> float | None:
    finals = [r.duration_sec for r in rows if r.name == "summarize_final"]
    return round(median(finals), 1) if finals else None


def main() -> None:
    rows, window_totals = asyncio.run(_collect_rows())
    weights = aggregate_step_weights(rows)

    # summarize_windows is stored per-window; app.js expects "per whole step"
    # (it divides by window count at runtime). Reconstruct a representative
    # per-step value using the median window count across runs.
    typical_windows = int(median([float(w) for w in window_totals])) if window_totals else 0
    per_window = weights.get("summarize_windows")
    summarize_windows_per_step = (
        round(per_window * typical_windows, 1)
        if per_window is not None and typical_windows >= 1
        else None
    )

    fallback = _final_summary_fallback(rows)

    # Collect entries first so we know which is the actual last printed value.
    # NO-DATA steps emit a comment line but must not affect comma placement.
    no_data_comments: dict[str, str] = {}
    data_entries: list[tuple[str, float, int]] = []
    for name in _PRINT_ORDER:
        if name == "summarize_windows":
            val = summarize_windows_per_step
        else:
            val = weights.get(name)
        n = _count(rows, name)
        if val is None:
            no_data_comments[name] = f"  // {name}: NO DATA (n=0) — keep existing value"
        else:
            data_entries.append((name, val, n))

    print("// --- paste into vts/static/app.js (STEP_WEIGHT_SECONDS) ---")
    print("const STEP_WEIGHT_SECONDS = {")
    last_data_name = data_entries[-1][0] if data_entries else None
    for name in _PRINT_ORDER:
        if name in no_data_comments:
            print(no_data_comments[name])
        else:
            entry = next(e for e in data_entries if e[0] == name)
            _name, val, n = entry
            comma = "" if name == last_data_name else ","
            print(f"  {name}: {val}{comma}  // n={n}")
    print("};")
    if summarize_windows_per_step is not None:
        print(
            f"// summarize_windows per-window median = {per_window} s "
            f"(typical {typical_windows} windows -> {summarize_windows_per_step} s/step)"
        )
    if fallback is not None:
        n_final = sum(1 for r in rows if r.name == "summarize_final")
        print(f"const FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS = {fallback};  // n={n_final}")
    else:
        print("// FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS: NO DATA — keep existing value")


if __name__ == "__main__":
    main()

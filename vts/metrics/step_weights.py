"""Pure aggregation of per-step progress-bar weights from run durations.

DB-free and deterministic so both the one-off recompute script (vts-b6t) and
the per-user followup (vts-8cm) can reuse it. Median, not mean, for robustness.
Supports per-user window offset for adjusted divisors when summarizing.
"""
from __future__ import annotations

from collections import namedtuple

StepDuration = namedtuple("StepDuration", ["name", "duration_sec", "window_total"])

# Step whose duration scales with the number of summary windows; stored per-window.
_PER_WINDOW_STEP = "summarize_windows"


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _per_window_divisor(window_total, window_offset: int) -> int | None:
    if not isinstance(window_total, int):
        return None
    divisor = window_total - window_offset
    return divisor if divisor >= 1 else None


def aggregate_step_weights(rows: list[StepDuration], *, window_offset: int = 0) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            divisor = _per_window_divisor(row.window_total, window_offset)
            if divisor is None:
                continue
            buckets.setdefault(row.name, []).append(row.duration_sec / divisor)
        else:
            buckets.setdefault(row.name, []).append(row.duration_sec)
    return {name: round(median(vals), 1) for name, vals in buckets.items() if vals}


def step_sample_counts(rows: list[StepDuration], *, window_offset: int = 0) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            if _per_window_divisor(row.window_total, window_offset) is None:
                continue
        counts[row.name] = counts.get(row.name, 0) + 1
    return counts


def merge_with_seed(
    computed: dict[str, float],
    sample_counts: dict[str, int],
    *,
    min_samples: int,
    seed: dict[str, float],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for step, seed_value in seed.items():
        if sample_counts.get(step, 0) >= min_samples and step in computed:
            out[step] = computed[step]
        else:
            out[step] = seed_value
    return out


def final_summary_fallback(rows: list[StepDuration], *, min_samples: int, seed_fallback: float) -> float:
    finals = [r.duration_sec for r in rows if r.name == "summarize_final"]
    if len(finals) >= min_samples:
        return round(median(finals), 1)
    return seed_fallback


# Seed weights recomputed one-off in vts-b6t (medians over completed runs,
# 2026-06-28). summarize_windows is per REAL window (total-1 convention).
SEED_STEP_WEIGHTS: dict[str, float] = {
    "download": 5.5,
    "extract_audio": 2.0,
    "trim_initial_silence": 0.3,
    "segment_audio": 1.2,
    "detect_language": 2.6,
    "transcribe_segments": 174.8,
    "merge_transcript": 0.1,
    "prepare_llama_model": 6.3,
    "prepare_summary_chunks": 0.1,
    "summarize_windows": 74.8,
}
SEED_FINAL_SUMMARY_FALLBACK: float = 514.4

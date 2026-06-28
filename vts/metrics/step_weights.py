"""Pure aggregation of per-step progress-bar weights from run durations.

DB-free and deterministic so both the one-off recompute script (vts-b6t) and
the per-user followup (vts-8cm) can reuse it. Median, not mean, for robustness.
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


def aggregate_step_weights(rows: list[StepDuration]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        if row.name == _PER_WINDOW_STEP:
            total = row.window_total
            if not isinstance(total, int) or total < 1:
                continue
            buckets.setdefault(row.name, []).append(row.duration_sec / total)
        else:
            buckets.setdefault(row.name, []).append(row.duration_sec)
    return {name: round(median(vals), 1) for name, vals in buckets.items() if vals}

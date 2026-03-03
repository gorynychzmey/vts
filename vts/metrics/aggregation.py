"""Aggregation helpers: percentiles, worst-N, task-level summary."""
from __future__ import annotations

from typing import Any


def compute_percentile(values: list[float], p: float) -> float:
    """Linear interpolation percentile (p in [0, 100])."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (k - lo)


def compute_worst_n(
    events: list[dict[str, Any]],
    key: str,
    n: int,
) -> list[dict[str, Any]]:
    """Return up to n events with the highest value for *key*."""
    filtered = [e for e in events if e.get(key) is not None]
    filtered.sort(key=lambda e: e[key], reverse=True)  # type: ignore[arg-type]
    return [
        {
            "segment_id": e.get("segment_id"),
            "stage": e.get("stage"),
            key: e[key],
        }
        for e in filtered[:n]
    ]


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    return round(compute_percentile(values, p), 4)


def aggregate_task_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute task-level aggregates from all emitted metric events."""
    # Wall time per stage
    stage_wall_ms: dict[str, int] = {}
    for e in events:
        stage = e.get("stage", "")
        t = e.get("t_wall_ms")
        if isinstance(t, (int, float)) and stage not in ("task.final",):
            stage_wall_ms[stage] = stage_wall_ms.get(stage, 0) + int(t)

    transcribe_events = [e for e in events if e.get("stage") == "transcribe.segment"]
    summarize_events = [
        e for e in events
        if e.get("stage") in ("summarize.segment", "summarize.global")
    ]

    rtf_values = [
        float(e["rtf"])
        for e in transcribe_events
        if e.get("rtf") is not None
    ]
    tok_per_s_values = [
        float(e["llm_tok_per_s"])
        for e in summarize_events
        if e.get("llm_tok_per_s") is not None
    ]
    cr_values = [
        float(e["compression_ratio"])
        for e in summarize_events
        if e.get("compression_ratio") is not None
    ]
    red_values = [
        float(e["redundancy_dup_sentence_ratio"])
        for e in summarize_events
        if e.get("redundancy_dup_sentence_ratio") is not None
    ]

    return {
        "total_wall_ms_by_stage": stage_wall_ms,
        "p50_rtf": _pct(rtf_values, 50),
        "p95_rtf": _pct(rtf_values, 95),
        "p50_llm_tok_per_s": _pct(tok_per_s_values, 50),
        "p95_llm_tok_per_s": _pct(tok_per_s_values, 95),
        "p50_compression_ratio": _pct(cr_values, 50),
        "p95_compression_ratio": _pct(cr_values, 95),
        "p50_redundancy_dup_sentence_ratio": _pct(red_values, 50),
        "p95_redundancy_dup_sentence_ratio": _pct(red_values, 95),
        "worst3_number_mismatch": compute_worst_n(summarize_events, "number_mismatch_count", 3),
        "worst3_redundancy": compute_worst_n(summarize_events, "redundancy_dup_sentence_ratio", 3),
    }

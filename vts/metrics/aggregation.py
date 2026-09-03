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


def _rtf(work_s: float, audio_s: float) -> float | None:
    """Processing time over audio duration, or None when it cannot be known.

    None rather than 0.0 for missing data: a zero RTF reads as "infinitely
    fast" in any chart or comparison, which is worse than an absent value.
    """
    if audio_s <= 0 or work_s <= 0:
        return None
    return round(work_s / audio_s, 4)


def aggregate_task_metrics(
    events: list[dict[str, Any]],
    *,
    stage_wall_overrides: dict[str, int] | None = None,
) -> dict[str, Any]:  # noqa: D417
    """Compute task-level aggregates from all emitted metric events.

    The ELAPSED time of a stage is normally already in the stream: the
    processor emits one event per step, named after the step
    (`transcribe_segments`), carrying the time it actually took. That differs
    from the sum of the per-segment events when segments run in parallel, and
    both numbers matter — see the two RTFs below. `stage_wall_overrides` exists
    for callers that time a stage themselves, and for tests.
    """
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

    # RTF per stage. Both variants are reported because they answer different
    # questions and, when a stage runs in parallel, differ by the parallelism
    # factor: measured on production, transcription is usually sequential
    # (both 0.058) but sometimes runs two-way (0.058 work vs 0.028 wall).
    overrides = stage_wall_overrides or {}
    tr_audio = sum(
        float(e.get("audio_duration_s") or 0) for e in transcribe_events
    )
    tr_work_ms = sum(float(e.get("t_wall_ms") or 0) for e in transcribe_events)
    # The step event, not the segment events: `transcribe_segments` is emitted
    # once by the processor with the step's real elapsed time.
    tr_wall_ms = float(
        overrides.get("transcribe.segment")
        or stage_wall_ms.get("transcribe_segments")
        or tr_work_ms
    )

    diarize_events = [e for e in events if e.get("stage") == "diarize.run"]
    di_audio = sum(float(e.get("audio_duration_s") or 0) for e in diarize_events)
    di_wall_ms = sum(float(e.get("t_wall_ms") or 0) for e in diarize_events)

    return {
        "total_wall_ms_by_stage": stage_wall_ms,
        # Audio duration is kept alongside: an RTF without the length it was
        # measured over cannot be sanity-checked or re-derived later.
        "transcribe_audio_s": round(tr_audio, 3) if tr_audio > 0 else None,
        "transcribe_rtf_work": _rtf(tr_work_ms / 1000.0, tr_audio),
        "transcribe_rtf_wall": _rtf(tr_wall_ms / 1000.0, tr_audio),
        # Segment work over step elapsed. ABOVE 1 means segments overlapped
        # (measured: 2.01 on a two-way run). BELOW 1 is not an error — it is
        # the step doing work outside the segments: on one production task the
        # step took 742s against 579s of segments, the 162s difference being
        # stitching results and writing artifacts, plus 6 ASR retries.
        "transcribe_work_over_wall": (
            round(tr_work_ms / tr_wall_ms, 2) if tr_wall_ms > 0 else None
        ),
        # One number only: diarization processes the WHOLE audio in one pass,
        # so there is no per-segment work to sum. Its "segments" are speech
        # boundaries, not units of processing.
        "diarize_audio_s": round(di_audio, 3) if di_audio > 0 else None,
        "diarize_rtf": _rtf(di_wall_ms / 1000.0, di_audio),
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

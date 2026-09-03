"""RTF for transcription and diarization: how fast we chew through audio.

RTF is processing time over audio duration — 0.06 means 17x faster than
real time. Two different RTFs exist for a step that runs segments in
parallel, and they answer different questions:

  * work RTF  = sum of segment processing time / audio duration
    Comparable across tasks and backends: it measures the model.
  * wall RTF  = elapsed step time / audio duration
    What the user actually waited for.

Measured on 193 production tasks: transcription is usually sequential and
the two agree (0.058), but one task in ten ran two-way parallel and they
differ by 2x (0.058 vs 0.028). Reporting only one would mislead, so both ship.
"""
from __future__ import annotations

from vts.metrics.aggregation import aggregate_task_metrics


def _seg(audio_s: float, wall_ms: int, rtf: float | None = None) -> dict:
    return {
        "stage": "transcribe.segment",
        "audio_duration_s": audio_s,
        "t_wall_ms": wall_ms,
        "rtf": rtf if rtf is not None else (wall_ms / 1000.0) / audio_s,
    }


def test_transcribe_rtf_reports_work_and_wall_separately():
    """Two segments of 100s each, 5s of work apiece, done in 5s wall.

    work RTF is 10/200 = 0.05; wall RTF is 5/200 = 0.025. A single number
    cannot be both.
    """
    events = [
        _seg(100.0, 5000),
        _seg(100.0, 5000),
        {"stage": "task.final", "t_wall_ms": 12000},
    ]
    agg = aggregate_task_metrics(events, stage_wall_overrides={"transcribe.segment": 5000})
    assert agg["transcribe_audio_s"] == 200.0
    assert agg["transcribe_rtf_work"] == 0.05
    assert agg["transcribe_rtf_wall"] == 0.025
    assert agg["transcribe_work_over_wall"] == 2.0


def test_sequential_step_gives_identical_rtfs():
    """With no parallelism the distinction vanishes, and parallelism is 1."""
    events = [_seg(100.0, 6000), {"stage": "task.final", "t_wall_ms": 9000}]
    agg = aggregate_task_metrics(events, stage_wall_overrides={"transcribe.segment": 6000})
    assert agg["transcribe_rtf_work"] == agg["transcribe_rtf_wall"] == 0.06
    assert agg["transcribe_work_over_wall"] == 1.0


def test_step_overhead_shows_as_ratio_below_one():
    """The step does work the segments do not account for.

    Real task: 742s of step against 579s of segments, the rest being result
    stitching, artifact writes and 6 ASR retries. That is a ratio of 0.78 —
    informative, not broken, so it must not be clamped or dropped.
    """
    events = [
        _seg(100.0, 5000),
        {"stage": "transcribe_segments", "t_wall_ms": 10000},
        {"stage": "task.final", "t_wall_ms": 11000},
    ]
    agg = aggregate_task_metrics(events)
    assert agg["transcribe_work_over_wall"] == 0.5
    # Wall RTF uses the STEP time, so it is the slower, honest number here.
    assert agg["transcribe_rtf_work"] == 0.05
    assert agg["transcribe_rtf_wall"] == 0.1


def test_diarization_rtf_is_whole_task_only():
    """Diarization sees the WHOLE audio, never per-chunk WAVs.

    That is deliberate (chunks are cut by duration, so the same person in
    two chunks would get two different speaker tags), which means there is
    no per-segment processing time to report — only one number for the run.
    """
    events = [
        {"stage": "diarize.run", "audio_duration_s": 600.0, "t_wall_ms": 120000, "rtf": 0.2},
        {"stage": "task.final", "t_wall_ms": 130000},
    ]
    agg = aggregate_task_metrics(events)
    assert agg["diarize_audio_s"] == 600.0
    assert agg["diarize_rtf"] == 0.2


def test_missing_data_yields_none_not_zero():
    """A task without diarization must not claim an RTF of 0.

    Zero would read as "infinitely fast" in any chart or comparison, which
    is worse than an absent value.
    """
    agg = aggregate_task_metrics([{"stage": "task.final", "t_wall_ms": 100}])
    assert agg["diarize_rtf"] is None
    assert agg["transcribe_rtf_work"] is None
    assert agg["transcribe_rtf_wall"] is None


def test_zero_length_audio_does_not_divide_by_zero():
    """A probe can report 0s (an empty or unreadable chunk)."""
    events = [
        {
            "stage": "transcribe.segment",
            "audio_duration_s": 0.0,
            "t_wall_ms": 500,
            "rtf": None,
        },
        {"stage": "task.final", "t_wall_ms": 1},
    ]
    agg = aggregate_task_metrics(events)
    assert agg["transcribe_rtf_work"] is None
    assert agg["transcribe_audio_s"] is None


def test_existing_percentiles_still_computed():
    """The new fields must not displace what the summary already carried."""
    events = [_seg(100.0, 5000), {"stage": "task.final", "t_wall_ms": 6000}]
    agg = aggregate_task_metrics(events)
    assert "p50_rtf" in agg and "p95_rtf" in agg
    assert "total_wall_ms_by_stage" in agg

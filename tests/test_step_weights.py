from vts.metrics.step_weights import (
    StepDuration,
    aggregate_step_weights,
    median,
    step_sample_counts,
    merge_with_seed,
    final_summary_fallback,
    SEED_STEP_WEIGHTS,
    SEED_FINAL_SUMMARY_FALLBACK,
)


def test_median_odd_and_even():
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_fixed_step_uses_median_duration():
    rows = [
        StepDuration("download", 10.0, None),
        StepDuration("download", 20.0, None),
        StepDuration("download", 30.0, None),
    ]
    assert aggregate_step_weights(rows) == {"download": 20.0}


def test_summarize_windows_normalized_per_window():
    # durations 100 over 10 windows -> 10/window; 60 over 6 -> 10/window
    rows = [
        StepDuration("summarize_windows", 100.0, 10),
        StepDuration("summarize_windows", 60.0, 6),
    ]
    assert aggregate_step_weights(rows) == {"summarize_windows": 10.0}


def test_summarize_windows_skips_rows_without_window_total():
    rows = [
        StepDuration("summarize_windows", 100.0, 10),  # 10/window
        StepDuration("summarize_windows", 999.0, 0),   # skipped (total < 1)
        StepDuration("summarize_windows", 999.0, None), # skipped
    ]
    assert aggregate_step_weights(rows) == {"summarize_windows": 10.0}


def test_step_with_no_rows_absent_from_result():
    rows = [StepDuration("download", 5.0, None)]
    result = aggregate_step_weights(rows)
    assert "extract_audio" not in result


def test_outlier_does_not_move_median_much():
    rows = [StepDuration("extract_audio", v, None) for v in (6.0, 7.0, 8.0, 6.5, 9000.0)]
    # median of 5 sorted values -> the middle one (7.0), outlier ignored
    assert aggregate_step_weights(rows) == {"extract_audio": 7.0}


def test_window_offset_divides_by_true_window_count():
    # 100s over total=11 -> with offset=1 divide by 10 -> 10.0/window
    rows = [StepDuration("summarize_windows", 100.0, 11)]
    assert aggregate_step_weights(rows, window_offset=1) == {"summarize_windows": 10.0}
    # default offset=0 keeps b6t behavior: divide by 11 -> 9.1
    assert aggregate_step_weights(rows) == {"summarize_windows": 9.1}


def test_window_offset_skips_when_true_count_below_one():
    rows = [StepDuration("summarize_windows", 50.0, 1)]  # offset=1 -> 0 -> skip
    assert aggregate_step_weights(rows, window_offset=1) == {}


def test_step_sample_counts_counts_valid_rows():
    rows = [
        StepDuration("download", 5.0, None),
        StepDuration("download", 6.0, None),
        StepDuration("summarize_windows", 100.0, 11),  # valid at offset=1
        StepDuration("summarize_windows", 50.0, 1),     # invalid at offset=1
    ]
    counts = step_sample_counts(rows, window_offset=1)
    assert counts["download"] == 2
    assert counts["summarize_windows"] == 1


def test_merge_with_seed_below_threshold_keeps_seed():
    seed = {"download": 5.5, "extract_audio": 2.0}
    computed = {"download": 99.0, "extract_audio": 88.0}
    counts = {"download": 10, "extract_audio": 2}
    merged = merge_with_seed(computed, counts, min_samples=5, seed=seed)
    assert merged == {"download": 99.0, "extract_audio": 2.0}


def test_merge_with_seed_missing_computed_uses_seed():
    seed = {"download": 5.5, "merge_transcript": 0.1}
    merged = merge_with_seed({}, {}, min_samples=5, seed=seed)
    assert merged == seed


def test_final_summary_fallback_threshold():
    rows = [StepDuration("summarize_final", v, None) for v in (400.0, 500.0, 600.0)]
    # 3 < min_samples 5 -> seed
    assert final_summary_fallback(rows, min_samples=5, seed_fallback=514.4) == 514.4
    # >= threshold -> median
    assert final_summary_fallback(rows, min_samples=3, seed_fallback=514.4) == 500.0


def test_seed_constants_present():
    assert SEED_STEP_WEIGHTS["summarize_windows"] == 74.8
    assert SEED_STEP_WEIGHTS["transcribe_segments"] == 174.8
    assert SEED_FINAL_SUMMARY_FALLBACK == 514.4
    assert len(SEED_STEP_WEIGHTS) == 10

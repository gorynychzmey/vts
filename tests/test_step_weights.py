from vts.metrics.step_weights import StepDuration, aggregate_step_weights, median


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

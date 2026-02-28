from vts.core.failures import classify_failure_code


def test_classify_failure_code_live_not_started() -> None:
    code = classify_failure_code("ERROR: [youtube] abc: This live event will begin in a few moments.")
    assert code == "download_live_not_started"


def test_classify_failure_code_unknown_returns_none() -> None:
    code = classify_failure_code("ERROR: network timeout")
    assert code is None


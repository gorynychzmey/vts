"""Table-driven coverage for trim_repetitive_edges (vts-5xz Finding 2).

Before this file, `grep -rln "trim_repetitive_edges" tests/` found nothing:
the function sits on the undiarized path (zero-regression-critical — it must
stay behaviourally identical after being refactored to delegate to the
shared `trim_repetitive_units` core) with no test pinning its behaviour at
all. These tests exercise the shared core through its public entry point.
"""

import pytest

from vts.pipeline.steps.transcription import trim_repetitive_edges

HALLUCINATION = "Продолжение следует."


def _repeat(n: int) -> str:
    return " ".join([HALLUCINATION] * n)


class TestMinRepeatsBoundary:
    """min_repeats = 6: 5 repeats must survive untouched, 6 must trim."""

    def test_five_repeats_at_tail_do_not_trim(self) -> None:
        text = "Настоящая речь тут." + " " + _repeat(5)
        cleaned, meta = trim_repetitive_edges(text)
        assert cleaned == text
        assert meta["removed_head_sentences"] == 0
        assert meta["removed_tail_sentences"] == 0

    def test_six_repeats_at_tail_do_trim(self) -> None:
        text = "Настоящая речь тут." + " " + _repeat(6)
        cleaned, meta = trim_repetitive_edges(text)
        assert cleaned == "Настоящая речь тут."
        assert meta["removed_tail_sentences"] == 6
        assert meta["removed_head_sentences"] == 0


def test_repeats_at_both_ends_trim_independently() -> None:
    head = _repeat(7)
    tail = _repeat(8)
    middle = "Единственная настоящая фраза тут."
    text = f"{head} {middle} {tail}"
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == middle
    assert meta["removed_head_sentences"] == 7
    assert meta["removed_tail_sentences"] == 8
    assert meta["head_phrase"] == HALLUCINATION
    assert meta["tail_phrase"] == HALLUCINATION


def test_all_repeats_falls_back_to_original_text() -> None:
    # The all-repeats "empty fallback" path: trimming everything away would
    # leave nothing to show, which is worse than the (hallucinated) original,
    # so the function returns the untrimmed text. `meta` still reports what
    # WOULD have been removed — that's the contract callers rely on.
    text = _repeat(10)
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == text.strip()
    assert meta["removed_head_sentences"] == 10
    assert meta["removed_tail_sentences"] == 0


def test_unit_longer_than_64_chars_is_not_trimmed() -> None:
    # _normalize_token strips punctuation/whitespace; the length gate applies
    # to the NORMALIZED token, so pad with letters (not spaces) to clear 64.
    long_sentence = "а" * 70 + "."
    text = " ".join([long_sentence] * 8)
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == text
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


def test_unit_exactly_64_chars_is_trimmed() -> None:
    exactly_64 = "а" * 64
    sentence = exactly_64 + "."
    text = " ".join([sentence] * 8) + " Настоящая речь тут."
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == "Настоящая речь тут."
    assert meta["removed_head_sentences"] == 8
    assert meta["removed_tail_sentences"] == 0


def test_unit_normalizing_to_empty_is_not_trimmed() -> None:
    # "!!!." normalizes (strip non-word chars) to "" — the head/tail loops
    # must bail out on an empty normalized token rather than treating it as
    # a repeatable unit.
    text = " ".join(["!!!."] * 8)
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == text.strip()
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\t  "])
def test_empty_or_whitespace_input(text: str) -> None:
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == ""
    assert meta == {
        "removed_head_sentences": 0,
        "removed_tail_sentences": 0,
        "head_phrase": None,
        "tail_phrase": None,
    }


def test_single_sentence_is_untouched() -> None:
    text = "Единственное предложение."
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == text
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


def test_text_with_no_terminators_is_one_unit_and_untouched() -> None:
    # No [.!?…] anywhere: the whole string is a single "sentence" per the
    # split regex, so there's nothing to repeat and nothing to trim.
    text = "просто поток слов без знаков препинания вообще"
    cleaned, meta = trim_repetitive_edges(text)
    assert cleaned == text
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0

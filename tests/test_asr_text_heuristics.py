"""Tests for the ASR text heuristics in vts/pipeline/steps/transcription.py.

These four functions decide whether a transcribed segment is a Whisper
hallucination and, if so, whether a retry produced something better. They had
no tests at all, which made TranscribeSegmentsStep.run unsafe to refactor:
nothing would have caught a changed threshold. Written as characterisation
tests — they record what the code does today, thresholds included, so a
deliberate change shows up as a failing test rather than a silent drift in
transcript quality.
"""
from __future__ import annotations

import pytest

from vts.pipeline.steps.transcription import (
    is_probable_asr_hallucination,
    normalize_token,
    tail_prompt,
    transcript_quality_score,
    trim_repetitive_edges,
)


# --------------------------------------------------------------------------
# normalize_token
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Привет,", "привет"),
        ("Hello!", "hello"),
        ("ёжик", "ёжик"),  # ё must survive: it is listed explicitly in the regex
        ("...", ""),
        ("  Мир  ", "мир"),
    ],
)
def test_normalize_token_strips_punctuation_and_casefolds(raw: str, expected: str) -> None:
    assert normalize_token(raw) == expected


def test_normalize_token_drops_inner_punctuation() -> None:
    # Not a word-splitter: punctuation is removed, so a comma fuses the words.
    # Fine for counting repeats, which is all this feeds.
    assert normalize_token("Привет, мир!") == "приветмир"


# --------------------------------------------------------------------------
# is_probable_asr_hallucination
# --------------------------------------------------------------------------


def test_hallucination_empty_text_is_not_suspicious() -> None:
    assert is_probable_asr_hallucination("") is False
    assert is_probable_asr_hallucination("   \n  ") is False


def test_hallucination_needs_at_least_ten_tokens() -> None:
    """Short text is never flagged, however repetitive.

    The guard matters in practice: a legitimate one-word answer or a short
    interjection would otherwise trip every repetition signal at once.
    """
    assert is_probable_asr_hallucination("да " * 9) is False
    assert is_probable_asr_hallucination("да " * 10) is True


def test_hallucination_flags_a_single_repeated_token() -> None:
    assert is_probable_asr_hallucination("да " * 30) is True


def test_hallucination_flags_a_repeated_subtitle_credit() -> None:
    """The real-world case this heuristic exists for.

    Whisper emits a repeated subtitle credit over silence or noise; the same
    string repeated eight times is the shape seen in production.
    """
    assert is_probable_asr_hallucination("Субтитры сделал DimaTorzok. " * 8) is True


def test_hallucination_requires_two_signals_not_one() -> None:
    """One signal alone must not flag the text — the threshold is two of three.

    This text trips `repeated_edge` (five identical opening sentences) but
    keeps a healthy unique-token ratio, so it stays clean. Without this case
    the >= 2 threshold could be lowered to >= 1 and every other test here
    would still pass.
    """
    text = (
        "Да. Да. Да. Да. Да. Мы обсудили план работ и решили начать "
        "с аудита инфраструктуры завтра утром."
    )
    assert is_probable_asr_hallucination(text) is False


def test_hallucination_leaves_ordinary_speech_alone() -> None:
    text = (
        "мы обсудили план работ на следующий квартал и решили начать "
        "с аудита инфраструктуры а затем перейти к миграции сервисов"
    )
    assert is_probable_asr_hallucination(text) is False


def test_hallucination_tolerates_natural_repetition() -> None:
    """One frequent word is not enough — two of three signals are required."""
    text = (
        "это очень важно и это очень срочно потому что мы уже обсуждали "
        "сроки и договорились закончить работу до конца недели без задержек"
    )
    assert is_probable_asr_hallucination(text) is False


# --------------------------------------------------------------------------
# transcript_quality_score — the retry tie-breaker
# --------------------------------------------------------------------------


def test_quality_score_empty_is_zero() -> None:
    assert transcript_quality_score("") == 0.0
    assert transcript_quality_score("   ") == 0.0


def test_quality_score_is_length_times_unique_ratio() -> None:
    assert transcript_quality_score("а б в г д е ё ж з и") == pytest.approx(10.0)
    # Same length, one distinct token: 10 * (1/10).
    assert transcript_quality_score("да " * 10) == pytest.approx(1.0)


def test_quality_score_prefers_varied_text_over_repetitive_text() -> None:
    """The property TranscribeSegmentsStep relies on when judging a retry.

    A retry is accepted when it is not suspicious OR simply scores higher, so
    the ordering below is what makes "both are bad, keep the less bad one"
    resolve the right way.
    """
    varied = "мы обсудили план работ и решили начать с аудита инфраструктуры"
    repetitive = "да " * 20
    assert transcript_quality_score(varied) > transcript_quality_score(repetitive)


def test_quality_score_rewards_length_at_equal_variety() -> None:
    short = "один два три"
    long = "один два три четыре пять шесть"
    assert transcript_quality_score(long) > transcript_quality_score(short)


# --------------------------------------------------------------------------
# tail_prompt — the carry-over into the next segment
# --------------------------------------------------------------------------


def test_tail_prompt_returns_none_for_blank_text() -> None:
    assert tail_prompt("") is None
    assert tail_prompt("   \n ") is None


def test_tail_prompt_keeps_the_end_not_the_start() -> None:
    """Whisper is primed with what came *just before* the current segment."""
    assert tail_prompt("abcdef", max_chars=2) == "ef"


def test_tail_prompt_passes_short_text_through() -> None:
    assert tail_prompt("  привет мир  ") == "привет мир"


def test_tail_prompt_caps_at_max_chars() -> None:
    assert len(tail_prompt("я " * 2000) or "") == 800


# --------------------------------------------------------------------------
# trim_repetitive_edges
# --------------------------------------------------------------------------


def test_trim_repetitive_edges_blank_text() -> None:
    cleaned, meta = trim_repetitive_edges("   ")
    assert cleaned == ""
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


def test_trim_repetitive_edges_keeps_clean_text_intact() -> None:
    text = "Первое предложение. Второе предложение. Третье предложение."
    cleaned, meta = trim_repetitive_edges(text)
    assert "Первое предложение" in cleaned
    assert "Третье предложение" in cleaned
    assert meta["removed_head_sentences"] == 0
    assert meta["removed_tail_sentences"] == 0


def test_trim_repetitive_edges_never_returns_empty_for_nonempty_input() -> None:
    """A segment that is nothing but a repeated phrase must not vanish.

    Dropping it entirely would silently lose the time span from the
    transcript, so the original text is kept when trimming would empty it.
    """
    cleaned, _meta = trim_repetitive_edges("Субтитры сделал DimaTorzok. " * 10)
    assert cleaned.strip() != ""

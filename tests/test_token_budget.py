"""Tests for the adaptive token budgeting logic."""

from __future__ import annotations

import math

import pytest

from vts.pipeline.token_budget import (
    TokenBudgetConfig,
    clamp,
    compute_final_budget,
    compute_pack_budget,
    compute_segment_budget,
    fits_in_context,
)
from vts.pipeline.types import DAG_STEPS
from vts.services.summarizer import inject_budget_vars


# ---------------------------------------------------------------------------
# clamp
# ---------------------------------------------------------------------------


def test_clamp_returns_value_within_range() -> None:
    assert clamp(5.0, 3.0, 10.0) == 5


def test_clamp_returns_min_when_below() -> None:
    assert clamp(1.0, 3.0, 10.0) == 3


def test_clamp_returns_max_when_above() -> None:
    assert clamp(15.0, 3.0, 10.0) == 10


def test_clamp_returns_int() -> None:
    result = clamp(4.7, 1.0, 10.0)
    assert isinstance(result, int)
    assert result == 4


def test_clamp_degenerate_min_greater_than_max_returns_min() -> None:
    # When min > max, clamp should not crash and should return min
    result = clamp(5.0, 200.0, 55.0)
    assert result == 200


# ---------------------------------------------------------------------------
# compute_segment_budget (Stage A)
# ---------------------------------------------------------------------------


LEGACY_SEGMENT = dict(segment_ratio=0.40, segment_min_ratio=0.30, segment_max_ratio=0.55)


def test_segment_budget_typical() -> None:
    cfg = TokenBudgetConfig(**LEGACY_SEGMENT)
    # 1000 tokens * 0.40 = 400
    # min = max(ceil(1000 * 0.30), 200) = max(300, 200) = 300
    # max = min(floor(1000 * 0.55), 1800) = min(550, 1800) = 550
    # target = clamp(400, 300, 550) = 400
    target, min_out, max_out = compute_segment_budget(1000, cfg)
    assert target == 400
    assert min_out == 300
    assert max_out == 550


def test_segment_budget_min_floor_applied() -> None:
    cfg = TokenBudgetConfig(**LEGACY_SEGMENT)
    # Very small input: 100 tokens
    # raw = 40, min = max(ceil(30), 200) = 200, max = min(55, 1800) = 55
    # clamp(40, 200, 55) → degenerate: returns 200
    target, min_out, max_out = compute_segment_budget(100, cfg)
    assert min_out == 200   # floor wins over ratio
    assert max_out == 55
    assert target == 200    # clamp with min > max returns min


def test_segment_budget_max_cap_applied() -> None:
    cfg = TokenBudgetConfig(**LEGACY_SEGMENT)
    # Large input: 5000 tokens
    # raw = 5000 * 0.40 = 2000
    # min = max(ceil(5000 * 0.30), 200) = 1500
    # max = min(floor(5000 * 0.55), 1800) = min(2750, 1800) = 1800
    # target = clamp(2000, 1500, 1800) = 1800
    target, min_out, max_out = compute_segment_budget(5000, cfg)
    assert max_out == 1800          # cap applied
    assert target == 1800           # capped at max


def test_segment_budget_min_ratio_boundary() -> None:
    cfg = TokenBudgetConfig(**LEGACY_SEGMENT)
    # 500 tokens: raw=200, min=max(150, 200)=200, max=min(275, 1800)=275
    # target = clamp(200, 200, 275) = 200
    target, min_out, max_out = compute_segment_budget(500, cfg)
    assert min_out == 200
    assert max_out == 275
    assert target == 200


# ---------------------------------------------------------------------------
# compute_pack_budget (Stage B)
# ---------------------------------------------------------------------------


def test_pack_budget_typical() -> None:
    cfg = TokenBudgetConfig()
    # 4000 tokens
    # raw = 4000 * 0.90 = 3600
    # min = max(ceil(4000 * 0.80), 400) = max(3200, 400) = 3200
    # max = floor(4000 * 0.95) = 3800
    # target = clamp(3600, 3200, 3800) = 3600
    target, min_out, max_out = compute_pack_budget(4000, cfg)
    assert target == 3600
    assert min_out == 3200
    assert max_out == 3800


def test_pack_budget_min_floor_applied() -> None:
    cfg = TokenBudgetConfig()
    # Small batch: 200 tokens
    # raw = 180, min = max(ceil(160), 400) = 400, max = floor(190) = 190
    # target = clamp(180, 400, 190) → degenerate, returns 400
    target, min_out, max_out = compute_pack_budget(200, cfg)
    assert min_out == 400
    assert target == 400


# ---------------------------------------------------------------------------
# compute_final_budget (Stage C)
# ---------------------------------------------------------------------------


def test_final_budget_typical() -> None:
    cfg = TokenBudgetConfig()
    # 2000 tokens
    # raw = 2000 * 0.70 = 1400
    # min = ceil(2000 * 0.60) = 1200
    # max = floor(2000 * 0.80) = 1600
    # target = clamp(1400, 1200, 1600) = 1400
    target, min_out, max_out = compute_final_budget(2000, cfg)
    assert min_out == 1200
    assert max_out == 1600
    assert target == 1400


def test_final_budget_small_input() -> None:
    cfg = TokenBudgetConfig()
    # 100 tokens: raw=70, min=ceil(60)=60, max=floor(80)=80
    # target = clamp(70, 60, 80) = 70
    target, min_out, max_out = compute_final_budget(100, cfg)
    assert target == 70
    assert min_out == 60
    assert max_out == 80


# ---------------------------------------------------------------------------
# fits_in_context
# ---------------------------------------------------------------------------


def test_fits_in_context_fits() -> None:
    # prompt=500, input=1000, estimated_out=ceil(1000*0.80)=800, safety=768
    # total = 500 + 1000 + 800 + 768 = 3068 <= 32768
    cfg = TokenBudgetConfig(n_ctx=32768, safety_margin=768)
    assert fits_in_context(cfg, prompt_tokens=500, input_tokens=1000) is True


def test_fits_in_context_does_not_fit() -> None:
    # prompt=200, input=800, estimated_out=ceil(800*0.80)=640, safety=100
    # total = 200 + 800 + 640 + 100 = 1740 > 1000
    cfg = TokenBudgetConfig(n_ctx=1000, safety_margin=100)
    assert fits_in_context(cfg, prompt_tokens=200, input_tokens=800) is False


def test_fits_in_context_exact_boundary() -> None:
    # prompt=100, input=500, estimated_out=ceil(500*0.80)=400, safety=100
    # total = 100 + 500 + 400 + 100 = 1100 > 1100? no, == 1100 <= 1100
    cfg = TokenBudgetConfig(n_ctx=1100, safety_margin=100)
    assert fits_in_context(cfg, prompt_tokens=100, input_tokens=500) is True


def test_fits_in_context_one_over_boundary() -> None:
    cfg = TokenBudgetConfig(n_ctx=1099, safety_margin=100)
    assert fits_in_context(cfg, prompt_tokens=100, input_tokens=500) is False


# ---------------------------------------------------------------------------
# inject_budget_vars
# ---------------------------------------------------------------------------


def test_inject_budget_vars_replaces_all_placeholders() -> None:
    prompt = "Input: ${INPUT_WORDS}, Target: ${TARGET_WORDS}, Ratio: ${TARGET_RATIO}%"
    result = inject_budget_vars(
        prompt,
        input_tokens=1000,
        target_tokens=400,
        target_ratio=0.40,
    )
    assert result == "Input: 750, Target: 300, Ratio: 40%"


def test_inject_budget_vars_skips_none_values() -> None:
    prompt = "Target: ${TARGET_WORDS}, Other: ${INPUT_WORDS}"
    result = inject_budget_vars(prompt, target_tokens=400)
    assert "${TARGET_WORDS}" not in result
    assert "${INPUT_WORDS}" in result  # not substituted


def test_inject_budget_vars_no_placeholders_in_prompt() -> None:
    prompt = "No placeholders here."
    result = inject_budget_vars(prompt, input_tokens=100, target_tokens=40)
    assert result == "No placeholders here."


# ---------------------------------------------------------------------------
# Stage order correctness
# ---------------------------------------------------------------------------


def test_stage_order_pack_between_windows_and_final() -> None:
    windows_idx = DAG_STEPS.index("summarize_windows")
    pack_idx = DAG_STEPS.index("pack_window_notes")
    final_idx = DAG_STEPS.index("summarize_final")
    assert windows_idx < pack_idx < final_idx


def test_pack_window_notes_in_dag() -> None:
    assert "pack_window_notes" in DAG_STEPS


# ---------------------------------------------------------------------------
# TokenBudgetConfig defaults
# ---------------------------------------------------------------------------


def test_token_budget_config_defaults() -> None:
    cfg = TokenBudgetConfig()
    assert cfg.n_ctx == 32768
    assert cfg.safety_margin == 768
    assert cfg.segment_ratio == pytest.approx(0.78)
    assert cfg.segment_min_ratio == pytest.approx(0.65)
    assert cfg.segment_max_ratio == pytest.approx(0.90)
    assert cfg.segment_min_floor == 200
    assert cfg.segment_max_cap == 1800
    assert cfg.pack_ratio == pytest.approx(0.90)
    assert cfg.pack_min_ratio == pytest.approx(0.80)
    assert cfg.pack_max_ratio == pytest.approx(0.95)
    assert cfg.pack_min_floor == 400
    assert cfg.pack_batch_max_input_tokens == 12000
    assert cfg.final_ratio == pytest.approx(0.70)
    assert cfg.final_min_ratio == pytest.approx(0.60)
    assert cfg.final_max_ratio == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# Whole-transcript mode helpers (vts-o51)
# ---------------------------------------------------------------------------


def test_fits_whole_transcript_conservative_formula() -> None:
    from vts.pipeline.token_budget import fits_whole_transcript

    cfg = TokenBudgetConfig(n_ctx=10000, safety_margin=768)
    # prompt + 2*transcript + margin <= n_ctx
    assert fits_whole_transcript(cfg, prompt_tokens=200, transcript_tokens=4516)
    assert not fits_whole_transcript(cfg, prompt_tokens=200, transcript_tokens=4517)


def test_whole_transcript_possible_hard_check_uses_min_ratio() -> None:
    from vts.pipeline.token_budget import whole_transcript_possible

    cfg = TokenBudgetConfig(n_ctx=10000, safety_margin=768, segment_min_ratio=0.5)
    # prompt + transcript*(1+0.5) + margin <= n_ctx -> transcript <= 6021
    assert whole_transcript_possible(cfg, prompt_tokens=200, transcript_tokens=6021)
    assert not whole_transcript_possible(cfg, prompt_tokens=200, transcript_tokens=6100)


def test_derive_window_tokens_floor_cap_and_middle() -> None:
    from vts.pipeline.token_budget import derive_window_tokens

    # Tiny window -> floor 2000 (legacy behavior)
    small = TokenBudgetConfig(n_ctx=4096, safety_margin=768)
    assert derive_window_tokens(small, prompt_tokens=300, cap=8192) == 2000
    # Big window -> capped
    big = TokenBudgetConfig(n_ctx=114688, safety_margin=768)
    assert derive_window_tokens(big, prompt_tokens=300, cap=8192) == 8192
    # Middle -> (n_ctx - prompt - margin) // 2
    mid = TokenBudgetConfig(n_ctx=12000, safety_margin=768)
    assert derive_window_tokens(mid, prompt_tokens=232, cap=8192) == 5500


def test_uncap_segment_for_input_scales_max_cap() -> None:
    from vts.pipeline.token_budget import uncap_segment_for_input

    cfg = TokenBudgetConfig(segment_max_ratio=0.70, segment_max_cap=1800)
    # Small input: cap untouched, same object semantics preserved
    assert uncap_segment_for_input(cfg, 2000).segment_max_cap == 1800
    # Verbatim rewrite of a big window must not be squeezed to 1800 tokens
    big = uncap_segment_for_input(cfg, 60000)
    assert big.segment_max_cap == 42000
    assert cfg.segment_max_cap == 1800  # original not mutated


def test_is_context_overflow_error_positives_and_negatives() -> None:
    from vts.pipeline.token_budget import is_context_overflow_error

    positives = [
        "llama chat completion failed with HTTP 400 for http://x: the request "
        "exceeds the available context size. try increasing the context size",
        "This model's maximum context length is 8192 tokens",
        "input length exceeds context window",
        "HTTP 500: n_ctx exceeded, prompt too long",
        "prompt is too long: 130000 tokens > 114688 maximum",
    ]
    negatives = [
        "connection refused",
        "model not found",
        "llama chat completion failed after retries: ReadTimeout",
        "invalid JSON in response",
        "",
    ]
    for text in positives:
        assert is_context_overflow_error(text), text
    for text in negatives:
        assert not is_context_overflow_error(text), text

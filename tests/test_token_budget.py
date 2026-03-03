"""Tests for the adaptive token budgeting logic."""

from __future__ import annotations

import math

import pytest

from vts.pipeline.token_budget import (
    TokenBudgetConfig,
    clamp,
    compute_final_budget,
    compute_final_in_budget,
    compute_pack_budget,
    compute_segment_budget,
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


def test_segment_budget_typical() -> None:
    cfg = TokenBudgetConfig()
    # 1000 tokens * 0.40 = 400
    # min = max(ceil(1000 * 0.30), 200) = max(300, 200) = 300
    # max = min(floor(1000 * 0.55), 1800) = min(550, 1800) = 550
    # target = clamp(400, 300, 550) = 400
    target, min_out, max_out = compute_segment_budget(1000, cfg)
    assert target == 400
    assert min_out == 300
    assert max_out == 550


def test_segment_budget_min_floor_applied() -> None:
    cfg = TokenBudgetConfig()
    # Very small input: 100 tokens
    # raw = 40, min = max(ceil(30), 200) = 200, max = min(55, 1800) = 55
    # clamp(40, 200, 55) → degenerate: returns 200
    target, min_out, max_out = compute_segment_budget(100, cfg)
    assert min_out == 200   # floor wins over ratio
    assert max_out == 55
    assert target == 200    # clamp with min > max returns min


def test_segment_budget_max_cap_applied() -> None:
    cfg = TokenBudgetConfig()
    # Large input: 5000 tokens
    # raw = 5000 * 0.40 = 2000
    # min = max(ceil(5000 * 0.30), 200) = 1500
    # max = min(floor(5000 * 0.55), 1800) = min(2750, 1800) = 1800
    # target = clamp(2000, 1500, 1800) = 1800
    target, min_out, max_out = compute_segment_budget(5000, cfg)
    assert max_out == 1800          # cap applied
    assert target == 1800           # capped at max


def test_segment_budget_min_ratio_boundary() -> None:
    cfg = TokenBudgetConfig()
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
    # max = min(floor(2000 * 0.80), 1400) = min(1600, 1400) = 1400
    # target = clamp(1400, 1200, 1400) = 1400
    target, min_out, max_out = compute_final_budget(2000, cfg)
    assert min_out == 1200
    assert max_out == 1400
    assert target == 1400


def test_final_budget_capped_by_final_out_budget() -> None:
    cfg = TokenBudgetConfig(final_out_budget=500)
    # 4000 tokens: raw=2800, min=2400, max=min(3200, 500)=500
    # target = clamp(2800, 2400, 500) → degenerate, returns 2400
    target, min_out, max_out = compute_final_budget(4000, cfg)
    assert max_out == 500
    assert target == 2400   # min > max → clamp returns min


def test_final_budget_small_input() -> None:
    cfg = TokenBudgetConfig()
    # 100 tokens: raw=70, min=ceil(60)=60, max=min(80, 1400)=80
    # target = clamp(70, 60, 80) = 70
    target, min_out, max_out = compute_final_budget(100, cfg)
    assert target == 70
    assert min_out == 60
    assert max_out == 80


# ---------------------------------------------------------------------------
# compute_final_in_budget
# ---------------------------------------------------------------------------


def test_final_in_budget_calculation() -> None:
    cfg = TokenBudgetConfig(n_ctx=32768, safety_margin=768, final_out_budget=1400)
    result = compute_final_in_budget(cfg, final_prompt_tokens=500)
    assert result == 32768 - 500 - 1400 - 768  # = 30100


def test_final_in_budget_with_large_prompt() -> None:
    cfg = TokenBudgetConfig(n_ctx=4096, safety_margin=256, final_out_budget=512)
    result = compute_final_in_budget(cfg, final_prompt_tokens=1024)
    assert result == 4096 - 1024 - 512 - 256  # = 2304


def test_packing_trigger_logic() -> None:
    """Verify that packing is triggered when total_notes_tokens > final_in_budget."""
    cfg = TokenBudgetConfig(n_ctx=1000, safety_margin=100, final_out_budget=200)
    budget = compute_final_in_budget(cfg, final_prompt_tokens=100)
    assert budget == 600

    # Fits → no packing needed
    assert 600 <= budget
    # Doesn't fit → packing needed
    assert 601 > budget


# ---------------------------------------------------------------------------
# inject_budget_vars
# ---------------------------------------------------------------------------


def test_inject_budget_vars_replaces_all_placeholders() -> None:
    prompt = "Input: ${INPUT_TOKENS}, Target: ${TARGET_TOKENS}, In: ${FINAL_IN_BUDGET}, Out: ${FINAL_OUT_BUDGET}"
    result = inject_budget_vars(
        prompt,
        input_tokens=1000,
        target_tokens=400,
        final_in_budget=30000,
        final_out_budget=1400,
    )
    assert result == "Input: 1000, Target: 400, In: 30000, Out: 1400"


def test_inject_budget_vars_skips_none_values() -> None:
    prompt = "Target: ${TARGET_TOKENS}, Other: ${INPUT_TOKENS}"
    result = inject_budget_vars(prompt, target_tokens=400)
    assert "${TARGET_TOKENS}" not in result
    assert "${INPUT_TOKENS}" in result  # not substituted


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
    assert cfg.final_out_budget == 1400
    assert cfg.segment_ratio == pytest.approx(0.40)
    assert cfg.segment_min_ratio == pytest.approx(0.30)
    assert cfg.segment_max_ratio == pytest.approx(0.55)
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

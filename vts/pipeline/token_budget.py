"""Adaptive token budgeting for the summarization pipeline."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TokenBudgetConfig:
    """All token budget parameters — every field overridable via Settings."""

    # Model / context
    n_ctx: int = 32768
    safety_margin: int = 768
    final_out_budget: int = 1400

    # Stage A — segment summarization
    segment_ratio: float = 0.40
    segment_min_ratio: float = 0.30
    segment_max_ratio: float = 0.55
    segment_min_floor: int = 200
    segment_max_cap: int = 1800

    # Stage B — packing/dedup
    pack_ratio: float = 0.90
    pack_min_ratio: float = 0.80
    pack_max_ratio: float = 0.95
    pack_min_floor: int = 400
    pack_batch_max_input_tokens: int = 12000

    # Stage C — final synthesis
    final_ratio: float = 0.70
    final_min_ratio: float = 0.60
    final_max_ratio: float = 0.80


@dataclass
class SummarizationMetrics:
    """Observability record emitted after every LLM call in the pipeline."""

    stage_name: str
    input_tokens: int
    target_tokens: int
    actual_output_tokens: int
    packing_triggered: bool = False
    packing_pass_count: int = 0


def clamp(value: float, min_val: float, max_val: float) -> int:
    """Clamp *value* to [min_val, max_val] and return an int.

    When min_val > max_val (degenerate input), returns int(min_val) so the
    caller always gets a usable floor rather than a nonsensical result.
    """
    return int(max(min_val, min(value, max_val)))


def compute_segment_budget(
    input_tokens: int, cfg: TokenBudgetConfig
) -> tuple[int, int, int]:
    """Stage A budget computation.

    Returns (target_tokens, min_out, max_out).
    """
    raw_target = input_tokens * cfg.segment_ratio
    min_out = max(
        math.ceil(input_tokens * cfg.segment_min_ratio),
        cfg.segment_min_floor,
    )
    max_out = min(
        math.floor(input_tokens * cfg.segment_max_ratio),
        cfg.segment_max_cap,
    )
    target_tokens = clamp(raw_target, min_out, max_out)
    return target_tokens, min_out, max_out


def compute_pack_budget(
    input_tokens: int, cfg: TokenBudgetConfig
) -> tuple[int, int, int]:
    """Stage B budget computation for a single packing batch.

    Returns (target_tokens, min_out, max_out).
    """
    raw_target = input_tokens * cfg.pack_ratio
    min_out = max(
        math.ceil(input_tokens * cfg.pack_min_ratio),
        cfg.pack_min_floor,
    )
    max_out = math.floor(input_tokens * cfg.pack_max_ratio)
    target_tokens = clamp(raw_target, min_out, max_out)
    return target_tokens, min_out, max_out


def compute_final_budget(
    input_tokens: int, cfg: TokenBudgetConfig
) -> tuple[int, int, int]:
    """Stage C budget computation.

    Returns (target_tokens, min_out, max_out).
    """
    raw_target = input_tokens * cfg.final_ratio
    min_out = math.ceil(input_tokens * cfg.final_min_ratio)
    max_out = min(
        math.floor(input_tokens * cfg.final_max_ratio),
        cfg.final_out_budget,
    )
    target_tokens = clamp(raw_target, min_out, max_out)
    return target_tokens, min_out, max_out


def compute_final_in_budget(cfg: TokenBudgetConfig, final_prompt_tokens: int) -> int:
    """Return the maximum token budget available for the final stage's input.

    final_in_budget = n_ctx − final_prompt_tokens − final_out_budget − safety_margin
    """
    return cfg.n_ctx - final_prompt_tokens - cfg.final_out_budget - cfg.safety_margin

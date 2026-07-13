"""Adaptive token budgeting for the summarization pipeline."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace


@dataclass
class TokenBudgetConfig:
    """All token budget parameters — every field overridable via Settings."""

    # Model / context
    n_ctx: int = 32768
    safety_margin: int = 768

    # Stage A — segment rewrite (verbatim-smooth since vts-3sj: the target is
    # the input minus fillers/repetitions, not a synopsis — hence high ratios;
    # ${TARGET_WORDS} in segment_prompt.md anchors the model to this length)
    segment_ratio: float = 0.78
    segment_min_ratio: float = 0.65
    segment_max_ratio: float = 0.90
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
    max_out = math.floor(input_tokens * cfg.final_max_ratio)
    target_tokens = clamp(raw_target, min_out, max_out)
    return target_tokens, min_out, max_out


# Legacy fixed window size; also the lower bound for the derived one.
SEGMENT_WINDOW_FLOOR = 2000


def fits_whole_transcript(
    cfg: TokenBudgetConfig, prompt_tokens: int, transcript_tokens: int
) -> bool:
    """Conservative whole-transcript check for segmentation mode ``auto``.

    A verbatim-smooth rewrite outputs roughly as much as it reads, so the
    transcript counts twice (input + output)."""
    return prompt_tokens + 2 * transcript_tokens + cfg.safety_margin <= cfg.n_ctx


def whole_transcript_possible(
    cfg: TokenBudgetConfig, prompt_tokens: int, transcript_tokens: int
) -> bool:
    """Hard feasibility check for segmentation mode ``never``.

    Uses the minimal expected output (segment_min_ratio) instead of the
    conservative 1:1 estimate. Below this bound the request cannot succeed —
    and Ollama would silently truncate the prompt instead of erroring, so the
    caller must fail explicitly before sending."""
    estimated_out = math.ceil(transcript_tokens * cfg.segment_min_ratio)
    return (
        prompt_tokens + transcript_tokens + estimated_out + cfg.safety_margin
        <= cfg.n_ctx
    )


def derive_window_tokens(
    cfg: TokenBudgetConfig,
    prompt_tokens: int,
    *,
    cap: int,
    floor: int = SEGMENT_WINDOW_FLOOR,
) -> int:
    """Segment window size derived from the context window (when splitting).

    Half of the free context (input plus an equally sized output), clamped
    to [floor, cap]."""
    available = (cfg.n_ctx - prompt_tokens - cfg.safety_margin) // 2
    return clamp(available, floor, max(floor, cap))


def uncap_segment_for_input(
    cfg: TokenBudgetConfig, input_tokens: int
) -> TokenBudgetConfig:
    """Raise segment_max_cap when it would squeeze a big window's rewrite.

    The fixed cap (default 1800) predates derived/whole windows: a verbatim
    rewrite of a 8k+ token input must not be budgeted down to 1800 tokens.
    Returns cfg unchanged while the cap does not bind."""
    needed = math.ceil(input_tokens * cfg.segment_max_ratio)
    if needed <= cfg.segment_max_cap:
        return cfg
    return replace(cfg, segment_max_cap=needed)


# Substrings that unambiguously indicate a context-window overflow in error
# text from llama-server / Ollama / LiteLLM / OpenAI-compatible backends.
_CTX_OVERFLOW_HINTS = (
    "exceeds the available context",
    "exceeds context",
    "exceed the context",
    "context length exceeded",
    "maximum context length",
    "exceeds context window",
    "input length exceeds",
    "context window",
    "prompt is too long",
    "too many tokens",
    "greater than the context",
)


def is_context_overflow_error(message: object) -> bool:
    """Heuristic: does this backend error text mean 'prompt did not fit'?"""
    text = str(message or "").lower()
    if not text:
        return False
    if any(hint in text for hint in _CTX_OVERFLOW_HINTS):
        return True
    return "n_ctx" in text and ("exceed" in text or "too long" in text or "too large" in text)


def fits_in_context(
    cfg: TokenBudgetConfig,
    prompt_tokens: int,
    input_tokens: int,
) -> bool:
    """Return True if prompt + input + estimated output + safety margin fit in n_ctx.

    Estimated output = ceil(input_tokens * final_max_ratio), which is the
    worst-case output size for the final synthesis stage.
    """
    estimated_out = math.ceil(input_tokens * cfg.final_max_ratio)
    return prompt_tokens + input_tokens + estimated_out + cfg.safety_margin <= cfg.n_ctx

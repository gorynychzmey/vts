"""Speaker-matching decisions over cosine distances.

Distances come from pgvector `<=>` (cosine): smaller = more similar. Thresholds
are in the same unit, so `auto` is numerically SMALLER than `candidate`.
Vectors are unnormalised (measured 2026-07-17), so cosine is the only sane
operator — never L2.
"""
from __future__ import annotations

from enum import StrEnum


class MatchOutcome(StrEnum):
    auto = "auto"
    grey = "grey"
    miss = "miss"


def bucket(distance: float | None, *, auto: float, candidate: float) -> MatchOutcome:
    """Classify a nearest-fragment distance into the three UX outcomes.

    None means no candidate existed at all (empty registry / no sample in this
    model) — a miss, not an error.
    """
    if distance is None:
        return MatchOutcome.miss
    if distance <= auto:
        return MatchOutcome.auto
    if distance <= candidate:
        return MatchOutcome.grey
    return MatchOutcome.miss

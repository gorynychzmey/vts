"""Per-user progress-weight recompute (vts-8cm).

Orchestrates the pure metrics + repo persistence. Math lives in
vts.metrics.step_weights; SQL lives in vts.db.repo. This module only wires them.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.db.repo import Repo
from vts.metrics.step_weights import (
    SEED_FINAL_SUMMARY_FALLBACK,
    SEED_STEP_WEIGHTS,
    aggregate_step_weights,
    final_summary_fallback,
    merge_with_seed,
    step_sample_counts,
)

logger = logging.getLogger("vts.step_weights")

# Per-user durations are normalized per REAL window (total - 1), matching app.js.
_WINDOW_OFFSET = 1


async def recompute_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    min_samples: int,
    seed: dict[str, float] = SEED_STEP_WEIGHTS,
    seed_fallback: float = SEED_FINAL_SUMMARY_FALLBACK,
) -> bool:
    repo = Repo(session)
    rows = await repo.step_durations_for_user(user_id)
    if not rows:
        return False
    computed = aggregate_step_weights(rows, window_offset=_WINDOW_OFFSET)
    counts = step_sample_counts(rows, window_offset=_WINDOW_OFFSET)
    weights = merge_with_seed(computed, counts, min_samples=min_samples, seed=seed)
    fallback = final_summary_fallback(rows, min_samples=min_samples, seed_fallback=seed_fallback)
    await repo.upsert_user_step_weights(
        user_id, weights, fallback, datetime.now(tz=timezone.utc), counts
    )
    return True


async def recompute_all_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    min_samples: int,
    seed: dict[str, float] = SEED_STEP_WEIGHTS,
    seed_fallback: float = SEED_FINAL_SUMMARY_FALLBACK,
) -> int:
    async with session_factory() as session:
        repo = Repo(session)
        user_ids = await repo.users_with_completed_tasks()
    updated = 0
    for user_id in user_ids:
        try:
            async with session_factory() as session:
                wrote = await recompute_for_user(
                    session, user_id, min_samples=min_samples, seed=seed, seed_fallback=seed_fallback
                )
                await session.commit()
            if wrote:
                updated += 1
        except Exception:  # one user's failure must not abort the sweep
            logger.exception("step-weights recompute failed for user %s", user_id)
    logger.info("step-weights recompute done: %s/%s users updated", updated, len(user_ids))
    return updated

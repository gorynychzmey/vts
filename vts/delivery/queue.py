"""Delivery queueing: backoff schedule + enqueue-on-completion.

`enqueue_deliveries` is called once a task completes successfully; it reads
the task's `options["delivery"]` spec, resolves each named delivery target
for the task owner, and creates a `DeliveryAttempt` row per resolved target.
Unknown target names are skipped (with a warning) rather than raised —
submit-time validation should have already caught these, so hitting one here
is defensive and must not fail the task, which is already `completed`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

logger = logging.getLogger("vts.delivery")


def backoff_seconds(attempts: int, base: int, cap: int) -> int:
    """Exponential backoff, capped: base * 2**(attempts-1), never above cap."""
    return min(base * (2 ** (attempts - 1)), cap)


async def enqueue_deliveries(repo, task, *, max_attempts: int, now: datetime) -> int:
    """Create one DeliveryAttempt per resolved delivery target on `task`.

    Reads `task.options["delivery"]`, a list of `{deliver_to, variant?}`.
    Returns the number of attempts enqueued.
    """
    spec = (task.options or {}).get("delivery") or []
    if not isinstance(spec, list):
        return 0
    count = 0
    for item in spec:
        if not isinstance(item, dict):
            continue
        ref = item.get("deliver_to")
        if not ref:
            continue
        # `deliver_to` holds the target's id, not its name (vts-929), so a
        # rename between submit and completion cannot orphan this delivery.
        try:
            target_id = uuid.UUID(str(ref))
        except ValueError:
            logger.warning(
                "delivery ref %r on task %s is not a target id; skipping", ref, task.id
            )
            continue
        target = await repo.get_delivery_target(task.user_id, target_id)
        if target is None:
            logger.warning("delivery target %r not found for task %s; skipping", ref, task.id)
            continue
        variant = item.get("variant") or (target.config_json or {}).get("default_variant", "summary")
        await repo.create_delivery_attempt(
            task_id=task.id, target_id=target.id, adapter=target.adapter,
            variant=variant, max_attempts=max_attempts, next_attempt_at=now)
        count += 1
    return count

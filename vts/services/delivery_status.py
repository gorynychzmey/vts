"""Single source of delivery-status semantics. Pure functions over DeliveryStatus.

Mirrors vts/services/task_status.py for DeliveryAttempt rows. Each set encodes
EXACTLY one question asked elsewhere; sets that look alike but answer different
questions are kept apart deliberately (see comments) so they cannot be "unified"
into a bug later — the drift that bit TERMINAL_FOR_WAIT_STATUSES in vts-hdl.

The pivotal distinction here is `waiting_adapter` vs the error states: a plugin
that did not load is a TRANSIENT condition, not a failure. It must never be
rendered as an error, never count against max_attempts, and never be what
retry_delivery revives (it revives itself).
"""
from __future__ import annotations

from vts.db.models import DeliveryStatus

# Nothing further will happen on its own. `waiting_adapter` is NOT here: it
# leaves by itself once the plugin returns.
TERMINAL_STATUSES = {DeliveryStatus.delivered, DeliveryStatus.dead}
# Delivery is still going to happen without anyone intervening.
IN_FLIGHT_STATUSES = {
    DeliveryStatus.pending,
    DeliveryStatus.delivering,
    DeliveryStatus.waiting_adapter,
}
# What the consumer picks up on a tick. Parked rows are claimable so they wake
# up and retry once their adapter is back — see repo.claim_due_deliveries.
CLAIMABLE_STATUSES = {DeliveryStatus.pending, DeliveryStatus.waiting_adapter}
# What retry_delivery revives. Excludes `waiting_adapter` on purpose: it is not
# stuck, it is waiting, and forcing it to `pending` would only burn attempts
# against an adapter that is still missing.
RETRYABLE_STATUSES = {DeliveryStatus.dead}
# Something actually went wrong and a human may need to look. Identical members
# to RETRYABLE_STATUSES today, but a DIFFERENT question ("is this an error?" vs
# "can this be retried?") — keep them separate so either can evolve alone.
ERROR_STATUSES = {DeliveryStatus.dead}


def is_terminal(status: DeliveryStatus) -> bool:
    return status in TERMINAL_STATUSES


def is_in_flight(status: DeliveryStatus) -> bool:
    return status in IN_FLIGHT_STATUSES


def is_claimable(status: DeliveryStatus) -> bool:
    return status in CLAIMABLE_STATUSES


def can_retry(status: DeliveryStatus) -> bool:
    return status in RETRYABLE_STATUSES


def is_error(status: DeliveryStatus) -> bool:
    return status in ERROR_STATUSES


def is_waiting_for_adapter(status: DeliveryStatus) -> bool:
    """True when the delivery is parked because its plugin is not loaded.

    Distinct from is_error: surfaces as "waiting for plugin", not "failed".
    """
    return status is DeliveryStatus.waiting_adapter

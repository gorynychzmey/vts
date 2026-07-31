from __future__ import annotations

from vts.db.models import DeliveryStatus
from vts.services import delivery_status as ds


def test_every_status_is_classified():
    """A new DeliveryStatus must be deliberately classified, not silently ignored.

    Every value is either terminal or in-flight — that partition must stay total,
    which is what catches "added a status, forgot the predicates".
    """
    for status in DeliveryStatus:
        assert ds.is_terminal(status) or ds.is_in_flight(status), status


def test_terminal_and_in_flight_are_disjoint():
    assert not (ds.TERMINAL_STATUSES & ds.IN_FLIGHT_STATUSES)


def test_waiting_adapter_is_not_an_error():
    """The whole point of the status: a missing plugin is transient, not a failure."""
    assert ds.is_waiting_for_adapter(DeliveryStatus.waiting_adapter)
    assert not ds.is_error(DeliveryStatus.waiting_adapter)
    assert not ds.is_terminal(DeliveryStatus.waiting_adapter)
    assert ds.is_in_flight(DeliveryStatus.waiting_adapter)


def test_waiting_adapter_is_claimable_but_not_retryable():
    # Claimable: it must wake up when the adapter returns.
    assert ds.is_claimable(DeliveryStatus.waiting_adapter)
    # Not retryable: retry_delivery would only burn attempts on a missing adapter.
    assert not ds.can_retry(DeliveryStatus.waiting_adapter)


def test_claimable_set_matches_the_consumer_query():
    assert ds.CLAIMABLE_STATUSES == {
        DeliveryStatus.pending,
        DeliveryStatus.waiting_adapter,
    }


def test_error_and_retryable_states():
    assert ds.is_error(DeliveryStatus.dead)
    assert ds.can_retry(DeliveryStatus.dead)
    assert ds.is_terminal(DeliveryStatus.dead)
    assert ds.is_terminal(DeliveryStatus.delivered)
    assert not ds.is_error(DeliveryStatus.delivered)
    assert not ds.can_retry(DeliveryStatus.delivered)  # success is not retried


def test_delivering_is_in_flight_only():
    assert ds.is_in_flight(DeliveryStatus.delivering)
    assert not ds.is_claimable(DeliveryStatus.delivering)  # reaper's job, not claim's
    assert not ds.is_terminal(DeliveryStatus.delivering)

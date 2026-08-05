"""Load-time contract validation in the delivery registry (vts-9y7, part B).

The registry discovers adapters via entry points, which means the loaded code
is NOT under this repo's control: with the plugin loader (vts-j8gz) it is a
wheel pulled from a GitHub release at `latest`. If the core contract has moved
on and the plugin has not, an incompatible adapter used to fail deep inside
`deliver()` at delivery time. These tests pin the guarantee that it is caught
at load time instead, and that the operator can see WHY.
"""
from __future__ import annotations

import pytest

from vts.delivery import registry
from vts.delivery.contract import CONTRACT_VERSION, DeliveryResult


class _Base:
    """Shape of a valid adapter; subclasses vary the one thing under test."""

    name = "fake"
    contract_version = (1, 0)

    def config_schema(self): return {"type": "object"}
    def secret_keys(self): return ["token"]
    def connection_fields(self): return ["base_url"]
    async def deliver(self, payload, target): return DeliveryResult()


def _load(**adapters):
    """Run real discovery over fake entry points.

    Patches only the entry-point enumeration, so the validation code under
    test is the real one.
    """
    class _EP:
        def __init__(self, name, factory):
            self.name = name
            self._factory = factory

        def load(self):
            return self._factory

    eps = [_EP(name, factory) for name, factory in adapters.items()]
    return eps


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", None, raising=False)
    monkeypatch.setattr(registry, "_INCOMPATIBLE", None, raising=False)


def _discover(monkeypatch, **adapters):
    monkeypatch.setattr(registry, "_entry_points", lambda: _load(**adapters))
    return registry.list_adapters(), registry.incompatible_adapters()


def test_compatible_adapter_is_registered(monkeypatch):
    ok, bad = _discover(monkeypatch, fake=_Base)
    assert set(ok) == {"fake"}
    assert bad == {}


def test_older_minor_is_accepted(monkeypatch):
    """min-compatible: the adapter states the MINIMUM core it needs, so a
    plugin built against 1.0 must keep working on a 1.5 core."""
    class Old(_Base):
        contract_version = (CONTRACT_VERSION[0], 0)

    ok, bad = _discover(monkeypatch, fake=Old)
    assert set(ok) == {"fake"}, bad


def test_newer_minor_is_rejected(monkeypatch):
    """The plugin needs a feature this core does not have yet."""
    class TooNew(_Base):
        contract_version = (CONTRACT_VERSION[0], CONTRACT_VERSION[1] + 3)

    ok, bad = _discover(monkeypatch, fake=TooNew)
    assert ok == {}
    assert "fake" in bad
    reason = bad["fake"]
    assert str(CONTRACT_VERSION[1] + 3) in reason and "minor" in reason.lower()


def test_foreign_major_is_rejected(monkeypatch):
    class OtherMajor(_Base):
        contract_version = (CONTRACT_VERSION[0] + 1, 0)

    ok, bad = _discover(monkeypatch, fake=OtherMajor)
    assert ok == {}
    assert "major" in bad["fake"].lower()


def test_missing_contract_version_is_rejected(monkeypatch):
    class NoVersion:
        name = "fake"

        def config_schema(self): return {}
        def secret_keys(self): return []
        def connection_fields(self): return []
        async def deliver(self, payload, target): return DeliveryResult()

    ok, bad = _discover(monkeypatch, fake=NoVersion)
    assert ok == {}
    assert "fake" in bad


@pytest.mark.parametrize(
    "value", ["1.0", 1, (1,), (1, "0"), None, (1, 2, 3)],
    ids=["str", "int", "short", "str-minor", "none", "long"],
)
def test_malformed_contract_version_is_rejected_with_a_clear_reason(monkeypatch, value):
    """A junk contract_version must not surface as an opaque crash.

    isinstance() against a runtime_checkable Protocol only checks that the
    attribute EXISTS, never its type — so `contract_version = "1.0"` sails
    through the Protocol check and then raises on indexing. Without an
    explicit shape check the operator sees a generic "load failed: string
    indices..." instead of being told the version is malformed.
    """
    class Junk(_Base):
        contract_version = value

    ok, bad = _discover(monkeypatch, fake=Junk)
    assert ok == {}
    assert "fake" in bad
    assert "contract_version" in bad["fake"], (
        f"reason must name the offending attribute, got: {bad['fake']!r}"
    )


def test_adapter_not_implementing_the_protocol_is_rejected(monkeypatch):
    class NotAnAdapter:
        name = "fake"
        contract_version = (1, 0)
        # no config_schema / secret_keys / deliver

    ok, bad = _discover(monkeypatch, fake=NotAnAdapter)
    assert ok == {}
    assert "fake" in bad


def test_one_broken_entry_point_does_not_hide_the_others(monkeypatch):
    """Failure isolation: discovery is over third-party code, so one bad
    plugin must not cost the operator every other adapter."""
    class Boom:
        def __init__(self):
            raise RuntimeError("kaboom")

    class Good(_Base):
        name = "good"

    ok, bad = _discover(monkeypatch, broken=Boom, good=Good)
    assert set(ok) == {"good"}
    assert "broken" in bad
    assert "kaboom" in bad["broken"]


def test_rejected_adapter_is_not_gettable(monkeypatch):
    class OtherMajor(_Base):
        contract_version = (CONTRACT_VERSION[0] + 1, 0)

    _discover(monkeypatch, fake=OtherMajor)
    with pytest.raises(registry.UnknownAdapter):
        registry.get_adapter("fake")


# --- published timing budget (vts-6o37 followup) ----------------------------


def test_timing_budget_is_published_and_coherent():
    """The budget an adapter should use must be strictly under the limit the
    core enforces. If they were equal, a plugin finishing exactly on its own
    deadline would still be cancelled — and everything it was about to report
    about the cause would be lost."""
    from vts.delivery.contract import ADAPTER_CALL_BUDGET_S, INTERACTIVE_CALL_LIMIT_S

    assert ADAPTER_CALL_BUDGET_S < INTERACTIVE_CALL_LIMIT_S, (
        "the adapter budget must leave headroom under the core's limit"
    )
    assert ADAPTER_CALL_BUDGET_S > 0


def test_core_enforces_the_published_limit_not_a_literal():
    """The whole point of publishing it: a plugin author reads the contract to
    size their HTTP client. A literal in the core would leave them guessing,
    and the two would drift — the same defect as the hard-coded variant enum
    a plugin used to carry (vts-6fya)."""
    import re
    from pathlib import Path

    from vts.delivery.contract import INTERACTIVE_CALL_LIMIT_S

    source = (Path(__file__).resolve().parents[2] / "vts" / "api" / "main.py").read_text()
    offenders = re.findall(r"wait_for\([^)]*timeout=\d", source)
    assert not offenders, (
        f"interactive adapter calls must use the published limit, not a literal: {offenders}"
    )
    assert INTERACTIVE_CALL_LIMIT_S > 0

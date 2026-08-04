from __future__ import annotations

import logging
from importlib.metadata import entry_points

from vts.delivery.contract import CONTRACT_VERSION, DeliveryAdapter

_CACHE: dict[str, DeliveryAdapter] | None = None
_INCOMPATIBLE: dict[str, str] | None = None

_log = logging.getLogger(__name__)


class UnknownAdapter(KeyError):
    """No delivery adapter registered under this name."""


def _entry_points():
    """Enumerate the `vts.delivery` entry points (seam for tests)."""
    return entry_points(group="vts.delivery")


def _version_problem(value: object) -> str | None:
    """Return why `value` is not a usable contract_version, or None if it is.

    The shape is checked explicitly rather than left to the Protocol: an
    isinstance() check against a runtime_checkable Protocol verifies only that
    the attribute EXISTS, never its type. Without this, an adapter declaring
    `contract_version = "1.0"` would pass the Protocol check and then raise on
    indexing, and the operator would be told "load failed: string indices must
    be integers" instead of what is actually wrong.
    """
    if value is None:
        return "does not declare contract_version"
    if not isinstance(value, tuple) or len(value) != 2:
        return f"contract_version must be a (major, minor) tuple, got {value!r}"
    if not all(isinstance(part, int) for part in value):
        return f"contract_version must hold integers, got {value!r}"
    return None


def _compatibility_problem(version: tuple[int, int]) -> str | None:
    """Return why `version` is incompatible with this core, or None if it fits.

    min-compatible: the adapter declares the MINIMUM contract it needs, so a
    plugin built against an older minor keeps working on a newer core, while a
    plugin needing a newer minor (or a different major) is refused.
    """
    major, minor = version
    if major != CONTRACT_VERSION[0]:
        return (
            f"needs contract major {major}, core provides {CONTRACT_VERSION[0]}"
        )
    if minor > CONTRACT_VERSION[1]:
        return (
            f"needs contract minor >= {minor}, core provides {CONTRACT_VERSION[1]}"
        )
    return None


def _discover() -> tuple[dict[str, DeliveryAdapter], dict[str, str]]:
    """Load and validate every advertised adapter.

    Returns (usable, rejected-with-reason). A rejected adapter is kept out of
    the registry but recorded, so an operator can see WHY an expected target
    stopped working instead of watching it vanish silently. Any single entry
    point may be third-party code pulled from a release at `latest`, so each
    one is isolated: its failure must not cost us the others.
    """
    usable: dict[str, DeliveryAdapter] = {}
    rejected: dict[str, str] = {}

    for ep in _entry_points():
        # Falls back to the entry-point name: an adapter that fails before we
        # can read `.name` still has to be reportable.
        label = ep.name
        try:
            adapter = ep.load()()
            label = getattr(adapter, "name", None) or ep.name

            problem = _version_problem(getattr(adapter, "contract_version", None))
            if problem is None:
                problem = _compatibility_problem(adapter.contract_version)
            if problem is None and not isinstance(adapter, DeliveryAdapter):
                problem = "does not implement the DeliveryAdapter contract"

            if problem is not None:
                rejected[label] = problem
                _log.warning("delivery adapter %r rejected: %s", label, problem)
                continue

            usable[adapter.name] = adapter
        except Exception as exc:  # noqa: BLE001 - third-party code, isolate it
            reason = f"failed to load: {exc}"
            rejected[label] = reason
            _log.warning("delivery adapter %r rejected: %s", label, reason)

    return usable, rejected


def _ensure_loaded() -> None:
    """Populate both caches once.

    The two are checked independently rather than together: callers (including
    tests) may seed just one of them, and re-running discovery in that case
    would silently discard what was seeded.
    """
    global _CACHE, _INCOMPATIBLE
    if _CACHE is not None:
        # Already discovered (or seeded by a caller). Nothing else may run
        # discovery: doing so would re-enter third-party plugin code and, when
        # only one of the two caches was seeded, throw the seeded one away.
        _INCOMPATIBLE = {} if _INCOMPATIBLE is None else _INCOMPATIBLE
        return
    _CACHE, _INCOMPATIBLE = _discover()


def list_adapters() -> dict[str, DeliveryAdapter]:
    _ensure_loaded()
    assert _CACHE is not None
    return dict(_CACHE)


def incompatible_adapters() -> dict[str, str]:
    """Adapters found but refused, keyed by name with the reason as value."""
    _ensure_loaded()
    assert _INCOMPATIBLE is not None
    return dict(_INCOMPATIBLE)


def get_adapter(name: str) -> DeliveryAdapter:
    adapters = list_adapters()
    try:
        return adapters[name]
    except KeyError as exc:
        raise UnknownAdapter(name) from exc

from __future__ import annotations

from importlib.metadata import entry_points

from vts.delivery.contract import DeliveryAdapter

_CACHE: dict[str, DeliveryAdapter] | None = None


class UnknownAdapter(KeyError):
    """No delivery adapter registered under this name."""


def _load_from_entry_points() -> dict[str, DeliveryAdapter]:
    out: dict[str, DeliveryAdapter] = {}
    for ep in entry_points(group="vts.delivery"):
        adapter_cls = ep.load()
        adapter = adapter_cls()
        out[adapter.name] = adapter
    return out


def list_adapters() -> dict[str, DeliveryAdapter]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_from_entry_points()
    return dict(_CACHE)


def get_adapter(name: str) -> DeliveryAdapter:
    adapters = list_adapters()
    try:
        return adapters[name]
    except KeyError as exc:
        raise UnknownAdapter(name) from exc

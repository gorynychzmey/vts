import pytest
from vts.delivery import registry
from vts.delivery.contract import DeliveryResult


class FakeAdapter:
    name = "fake"
    def config_schema(self): return {"type": "object"}
    def secret_keys(self): return ["token"]
    async def deliver(self, payload, target): return DeliveryResult()


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(registry, "_CACHE", None, raising=False)
    monkeypatch.setattr(registry, "_load_from_entry_points",
                        lambda: {"fake": FakeAdapter()})


def test_list_adapters_returns_registered():
    assert set(registry.list_adapters()) == {"fake"}


def test_get_adapter_found():
    assert registry.get_adapter("fake").name == "fake"


def test_get_adapter_unknown_raises():
    with pytest.raises(registry.UnknownAdapter):
        registry.get_adapter("nope")

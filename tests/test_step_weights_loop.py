import asyncio
import pytest
from vts.worker.main import _step_weights_tick
from vts.core.config import get_settings

pytestmark = pytest.mark.asyncio


async def test_tick_calls_recompute(monkeypatch):
    calls = {}

    async def fake_recompute(session_factory, *, min_samples, **kw):
        calls["min_samples"] = min_samples
        return 0

    monkeypatch.setattr("vts.worker.main.recompute_all_users", fake_recompute)
    await _step_weights_tick(min_samples=7)
    assert calls["min_samples"] == 7


async def test_settings_defaults():
    s = get_settings()
    assert s.progress_weights_enabled is True
    assert s.progress_weights_recompute_interval_seconds == 604800
    assert s.progress_weights_min_samples == 5

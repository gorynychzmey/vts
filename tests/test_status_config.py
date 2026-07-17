from __future__ import annotations

from vts.db.models import TaskStatus


async def test_status_config_returns_flags_for_every_status(client) -> None:
    response = await client.get("/api/status-config")

    assert response.status_code == 200
    flags = response.json()["status_flags"]
    assert set(flags) == {s.value for s in TaskStatus}


async def test_status_config_flag_values(client) -> None:
    response = await client.get("/api/status-config")

    flags = response.json()["status_flags"]
    assert flags["waiting"]["is_active"] is True
    assert flags["queued"]["shows_progress"] is False
    assert flags["running"]["can_pause"] is True
    assert flags["completed"]["is_finished"] is True
    assert flags["paused"]["can_resume"] is True


async def test_status_config_every_status_exposes_all_eight_keys(client) -> None:
    response = await client.get("/api/status-config")

    flags = response.json()["status_flags"]
    expected_keys = {
        "is_active",
        "is_pending",
        "is_finished",
        "shows_progress",
        "can_pause",
        "can_resume",
        "can_archive",
        "needs_input",
    }
    for status, entry in flags.items():
        assert set(entry) == expected_keys, status
        assert all(isinstance(v, bool) for v in entry.values()), status


async def test_status_config_is_not_cached(client) -> None:
    response = await client.get("/api/status-config")

    assert "no-store" in response.headers["cache-control"]

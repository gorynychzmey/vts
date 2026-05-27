"""Smoke checks on the generated /openapi.json — important for external
clients (GPT Custom Actions, curl, Postman) that depend on the spec."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def app_no_oauth(monkeypatch):
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "false")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.test")
    get_settings.cache_clear()
    from vts.api.main import create_app
    return create_app()


async def test_openapi_advertises_bearer_security_scheme(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "ApiToken" in schemes
    assert schemes["ApiToken"]["type"] == "http"
    assert schemes["ApiToken"]["scheme"] == "bearer"


async def test_openapi_has_servers_from_public_base_url(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    spec = r.json()
    assert spec.get("servers") == [{"url": "https://vts.test"}]


async def test_openapi_hides_internal_routes(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    # Browser-only / session-only / internal routes — external clients
    # have no business with them.
    for hidden in (
        "/auth/login", "/auth/callback", "/auth/logout",
        "/api/me/tokens", "/api/me/tokens/{token_id}",
        "/api/events",
        "/api/push/config", "/api/push/status",
        "/api/push/subscribe", "/api/push/unsubscribe",
        "/player/{task_id}",
        "/sw.js", "/manifest.webmanifest",
        "/healthz",  # internal liveness
    ):
        assert hidden not in paths, f"{hidden} should not be in the public OpenAPI spec"


async def test_openapi_exposes_core_task_routes(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    for required in (
        "/api/version",
        "/api/me",
        "/api/tasks",
        "/api/tasks/upload",
        "/api/tasks/{task_id}",
        "/api/tasks/{task_id}/transcript",
        "/api/tasks/{task_id}/summary",
        "/api/tasks/{task_id}/media",
    ):
        assert required in paths, f"{required} missing from public OpenAPI spec"


async def test_openapi_tags_routes_by_prefix(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    for op in paths.get("/api/tasks", {}).values():
        assert "tasks" in op.get("tags", [])
    for op in paths.get("/api/version", {}).values():
        assert "meta" in op.get("tags", [])

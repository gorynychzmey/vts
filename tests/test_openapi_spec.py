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


async def test_openapi_text_endpoints_declare_content_type(app_no_oauth) -> None:
    """transcript/summary/redacted/log return text — make that explicit so
    clients (incl. GPT Custom Actions) don't fall back to 'unknown'."""
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})

    for path, content_types in [
        ("/api/tasks/{task_id}/transcript", {"text/plain", "application/json"}),
        ("/api/tasks/{task_id}/summary", {"text/markdown", "application/json"}),
        ("/api/tasks/{task_id}/redacted", {"text/plain"}),
        ("/api/tasks/{task_id}/log", {"text/plain"}),
    ]:
        op = paths[path]["get"]
        ok = op["responses"]["200"]["content"]
        assert content_types.issubset(ok.keys()), (
            f"{path} should declare {content_types} content types, got {ok.keys()}"
        )


async def test_text_endpoints_expose_offset_limit_and_textslice(app_no_oauth) -> None:
    """transcript/summary/redacted/log advertise the offset+limit query params
    plus an application/json variant that references TextSliceOut, so GPT
    can paginate around its ~30KB response cap."""
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    spec = r.json()
    schemas = spec.get("components", {}).get("schemas", {})
    assert "TextSliceOut" in schemas
    for path in (
        "/api/tasks/{task_id}/transcript",
        "/api/tasks/{task_id}/summary",
        "/api/tasks/{task_id}/redacted",
        "/api/tasks/{task_id}/log",
    ):
        op = spec["paths"][path]["get"]
        param_names = {p["name"] for p in op.get("parameters", [])}
        assert {"offset", "limit"}.issubset(param_names), (path, param_names)
        ok_content = op["responses"]["200"]["content"]
        assert "application/json" in ok_content, path
        ref = ok_content["application/json"]["schema"].get("$ref", "")
        assert ref.endswith("/TextSliceOut"), (path, ref)


async def test_list_tasks_exposes_pagination_and_compact(app_no_oauth) -> None:
    """GET /api/tasks should advertise limit/offset/compact query params so
    constrained clients (GPT Actions, 30KB response cap) can chunk the list."""
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    spec = r.json()
    op = spec["paths"]["/api/tasks"]["get"]
    param_names = {p["name"] for p in op.get("parameters", [])}
    assert {"limit", "offset", "compact"}.issubset(param_names), param_names


async def test_openapi_tags_routes_by_prefix(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    for op in paths.get("/api/tasks", {}).values():
        assert "tasks" in op.get("tags", [])
    for op in paths.get("/api/version", {}).values():
        assert "meta" in op.get("tags", [])

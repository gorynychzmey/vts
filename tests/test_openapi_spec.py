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


def test_downgrade_inlines_single_branch_anyof_nullable() -> None:
    from vts.api.main import _downgrade_to_openapi_30
    schema = {
        "properties": {
            "source_title": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "title": "Source Title",
            }
        }
    }
    _downgrade_to_openapi_30(schema)
    field = schema["properties"]["source_title"]
    assert field["type"] == "string"
    assert field["nullable"] is True
    assert "anyOf" not in field


def test_downgrade_handles_union_type_list() -> None:
    from vts.api.main import _downgrade_to_openapi_30
    schema = {"foo": {"type": ["string", "null"]}}
    _downgrade_to_openapi_30(schema)
    assert schema["foo"]["type"] == "string"
    assert schema["foo"]["nullable"] is True


def test_downgrade_preserves_anyof_when_multiple_non_null_branches() -> None:
    from vts.api.main import _downgrade_to_openapi_30
    schema = {
        "x": {
            "anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}],
        }
    }
    _downgrade_to_openapi_30(schema)
    # Multiple non-null branches must stay in anyOf; nullable set on parent.
    assert schema["x"]["nullable"] is True
    types = {b["type"] for b in schema["x"]["anyOf"]}
    assert types == {"string", "integer"}


def test_downgrade_leaves_non_nullable_schemas_alone() -> None:
    from vts.api.main import _downgrade_to_openapi_30
    schema = {"id": {"type": "string", "format": "uuid"}}
    _downgrade_to_openapi_30(schema)
    assert schema == {"id": {"type": "string", "format": "uuid"}}


async def test_openapi_is_30_compat_no_31_nullable_forms(app_no_oauth) -> None:
    """ChatGPT Custom Actions reject responses validated against the 3.1
    nullable form (`anyOf: [..., {type: null}]`). Ensure we emit 3.0-style
    `nullable: true` instead, and advertise the older spec version."""
    import json as _json
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    spec = r.json()
    assert spec.get("openapi", "").startswith("3.0"), spec.get("openapi")
    body = _json.dumps(spec)
    # No 3.1-only null marker should survive the downgrade pass.
    assert '"type": "null"' not in body and '"type":"null"' not in body, (
        "OpenAPI 3.1 nullable form leaked through; ChatGPT will reject responses"
    )


async def test_openapi_operation_descriptions_fit_gpt_actions_limit(app_no_oauth) -> None:
    """ChatGPT Custom Actions reject operations whose `description` exceeds
    300 chars. Catch any new endpoint that drifts past the limit before it
    breaks the GPT import."""
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    offenders: list[str] = []
    for path, methods in paths.items():
        for verb, op in methods.items():
            if not isinstance(op, dict):
                continue
            desc = op.get("description", "") or ""
            if len(desc) > 300:
                offenders.append(f"{verb.upper()} {path}: {len(desc)} chars")
    assert not offenders, (
        "Operation descriptions over the 300-char ChatGPT Actions limit:\n  "
        + "\n  ".join(offenders)
    )


async def test_openapi_tags_routes_by_prefix(app_no_oauth) -> None:
    transport = ASGITransport(app=app_no_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/openapi.json")
    paths = r.json().get("paths", {})
    for op in paths.get("/api/tasks", {}).values():
        assert "tasks" in op.get("tags", [])
    for op in paths.get("/api/version", {}).values():
        assert "meta" in op.get("tags", [])

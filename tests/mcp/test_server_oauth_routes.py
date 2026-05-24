from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings_from_yaml(monkeypatch):
    """OAuth-route tests must control Settings purely via env vars,
    so monkeypatch the YAML loader away for the duration of each test.
    Without this, the dev-host config.yaml leaks mcp_* keys into Settings
    and the env-var-based assertions fail unpredictably."""
    from vts.core.config import get_settings

    monkeypatch.setattr("vts.core.config._load_yaml_overrides", lambda: {})
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_server_with_oauth(monkeypatch):
    """Construct a build_mcp_server() with OAuth env vars set."""
    from vts.core.config import Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("VTS_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setenv("VTS_MCP_OAUTH_BASE_URL", "https://vts.example/mcp")

    from vts.mcp.server import build_mcp_server

    server = build_mcp_server()
    yield server

    get_settings.cache_clear()


async def test_oauth_enabled_server_publishes_oauth_routes(monkeypatch) -> None:
    """build_mcp_server() with OAuth should expose /authorize, /token, /register, metadata."""
    gen = _build_server_with_oauth(monkeypatch)
    server = next(gen)
    app = server.http_app(path="/")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/.well-known/oauth-authorization-server" in paths
    assert any(p and p.startswith("/.well-known/oauth-protected-resource") for p in paths)
    assert "/register" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/auth/callback" in paths
    try:
        next(gen)
    except StopIteration:
        pass


async def test_oauth_disabled_server_has_no_oauth_routes(monkeypatch) -> None:
    """Without OAuth flag, no /authorize / /token routes are registered."""
    import vts.core.config as _cfg
    from vts.core.config import Settings, get_settings

    get_settings.cache_clear()
    # The YAML config (if present on this host) may have mcp_oauth_enabled=True with
    # constructor-kwarg precedence that beats env vars.  Patch _load_yaml_overrides so
    # Settings() sees an empty override dict, then the env-var "false" wins cleanly.
    monkeypatch.setattr(_cfg, "_load_yaml_overrides", lambda: {})
    monkeypatch.setenv("VTS_MCP_OAUTH_ENABLED", "false")

    from vts.mcp.server import build_mcp_server

    server = build_mcp_server()
    app = server.http_app(path="/")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/authorize" not in paths
    assert "/token" not in paths

    get_settings.cache_clear()

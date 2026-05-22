from __future__ import annotations


def test_fastmcp_importable() -> None:
    """Smoke test: fastmcp is installed and exposes FastMCP."""
    from fastmcp import FastMCP

    mcp = FastMCP(name="vts-test")
    assert mcp.name == "vts-test"


def test_build_mcp_app_returns_asgi_callable() -> None:
    from vts.mcp import build_mcp_app

    app = build_mcp_app()
    # ASGI app callable signature: scope, receive, send
    assert callable(app)


def test_webapi_mounts_mcp_when_enabled(monkeypatch) -> None:
    """The FastAPI app should have a route mounted at the configured mcp_path."""
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_MCP_ENABLED", "true")
    monkeypatch.setenv("VTS_MCP_PATH", "/mcp")
    get_settings.cache_clear()
    from vts.api.main import create_app

    app = create_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/mcp" in paths
    get_settings.cache_clear()


def test_webapi_does_not_mount_mcp_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_ENABLED", "false")
    # Settings is cached — clear the lru_cache so the env change takes effect.
    from vts.core.config import get_settings
    get_settings.cache_clear()
    from vts.api.main import create_app

    app = create_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/mcp" not in paths
    get_settings.cache_clear()

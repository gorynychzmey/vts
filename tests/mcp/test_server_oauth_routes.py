from __future__ import annotations


async def test_oauth_enabled_server_publishes_oauth_routes(monkeypatch) -> None:
    """build_mcp_server() with OAuth should expose /authorize, /token, /register, metadata."""
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "super-secret-value")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")

    from vts.mcp.server import build_mcp_server

    server = build_mcp_server()
    app = server.http_app(path="/")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/.well-known/oauth-authorization-server" in paths
    # FastMCP 3.3.1 registers oauth-protected-resource with an mcp-path suffix
    # (e.g. /.well-known/oauth-protected-resource/mcp), so prefix-match it.
    assert any(p and p.startswith("/.well-known/oauth-protected-resource") for p in paths)
    assert "/register" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    # FastMCP's Google redirect URI is /<mcp_path>/auth/callback so it
    # doesn't clash with the web UI's /auth/callback.
    assert "/mcp/auth/callback" in paths


async def test_oauth_disabled_server_has_no_oauth_routes(monkeypatch) -> None:
    """Without OAuth flag, no /authorize / /token routes are registered."""
    monkeypatch.delenv("VTS_MCP_OAUTH_ENABLED", raising=False)

    from vts.mcp.server import build_mcp_server

    server = build_mcp_server()
    app = server.http_app(path="/")
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/authorize" not in paths
    assert "/token" not in paths

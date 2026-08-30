from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from vts.core.config import get_settings
from vts.mcp.tools_registry import delivery, presets, prompts, recordings, search, tasks


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server with all MCP tools registered."""
    settings = get_settings()
    auth_provider = None
    if settings.oauth_enabled:
        from fastmcp.server.auth.providers.google import GoogleProvider

        if not settings.oauth_client_id or not settings.oauth_client_secret:
            raise RuntimeError(
                "oauth_enabled but client_id/client_secret missing — "
                "set VTS_OAUTH_CLIENT_ID and VTS_OAUTH_CLIENT_SECRET"
            )
        if not settings.public_base_url:
            raise RuntimeError(
                "oauth_enabled but public_base_url missing — "
                "set VTS_PUBLIC_BASE_URL (e.g. https://vts.example.com)"
            )
        # FastMCP's auth provider publishes /.well-known/oauth-* metadata
        # whose URLs are anchored to issuer_url's host (RFC 8414/9728: metadata
        # MUST live at the host root, not under a subpath). When the MCP app
        # is mounted at /mcp the well-known routes also need to be reachable
        # at the host root — see build_mcp_app() below, which extracts them
        # so the parent FastAPI can mount them on /.
        #
        # base_url stays host-only (no /mcp suffix): that's what the spec
        # calls the "resource server URL" and what well-known docs reference.
        # redirect_path is moved off /auth/callback (used by the web UI) to
        # /mcp/auth/callback, which is what the Google client already has
        # registered for MCP.
        auth_provider = GoogleProvider(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            base_url=settings.public_base_url.rstrip("/"),
            redirect_path=f"{settings.mcp_path.rstrip('/')}/auth/callback",
            required_scopes=["openid", "email"],
            require_authorization_consent="remember",
        )
    mcp = FastMCP(name="vts", auth=auth_provider)

    # Tool bodies live in vts/mcp/tools_registry/, one module per domain.
    # They only ever needed `mcp` from this scope, so registration is just
    # passing it in (docs/plans/main-py-split.md, same shape as the API split).
    for domain in (tasks, prompts, presets, delivery, search, recordings):
        domain.register(mcp)

    return mcp


def build_mcp_app_with_wellknown(mcp_path: str) -> tuple[Any, list]:
    """Build the ASGI app AND extract the FastMCP auth provider's
    OAuth routes that must live at host root.

    RFC 8414 + RFC 9728 require OAuth metadata to live at the resource's
    host root, not under a subpath. The metadata document also references
    /authorize, /token, /register, /consent and the redirect callback —
    all of which must therefore live at root too, otherwise clients hit
    the URL advertised by the metadata and get 404s from sub-app paths.

    FastMCP exposes these routes via `auth.get_routes(mcp_path=...)`; we
    return them ALL so the parent FastAPI mounts them on `/`. The MCP
    sub-app itself (mounted at mcp_path) is left with the JSON-RPC
    endpoint and nothing else auth-related — auth.get_routes(...) already
    omits the streamable-HTTP transport handler.

    Returns (asgi_app, oauth_routes). oauth_routes is an empty list when
    no auth provider is attached.
    """
    server = build_mcp_server()
    # path="/" mounts the streamable-HTTP endpoint at the sub-app root so
    # the external URL is /mcp (when the sub-app is mounted at /mcp) rather
    # than /mcp/mcp.
    app = server.http_app(path="/")
    routes: list = []
    if server.auth is not None:
        routes = list(server.auth.get_routes(mcp_path=mcp_path))
    return app, routes


def build_mcp_app() -> Any:
    """Legacy single-return accessor — used by callers that don't need the
    OAuth routes (e.g. when OAuth is off)."""
    app, _ = build_mcp_app_with_wellknown(mcp_path="/mcp")
    return app

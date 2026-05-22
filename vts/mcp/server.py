from __future__ import annotations

from typing import Any

from fastmcp import FastMCP


def build_mcp_server() -> FastMCP:
    """Construct the FastMCP server. Tools are registered in later tasks."""
    mcp = FastMCP(name="vts")
    return mcp


def build_mcp_app() -> Any:
    """Return an ASGI app suitable for mounting in FastAPI.

    FastMCP 3.x exposes a Streamable HTTP transport via `http_app()`.
    The app is mountable as a sub-app on any ASGI host.
    """
    mcp = build_mcp_server()
    return mcp.http_app()

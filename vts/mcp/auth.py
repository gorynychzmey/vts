from __future__ import annotations

from fastmcp.server.dependencies import get_http_request
from sqlalchemy.ext.asyncio import AsyncSession

from vts.core.config import Settings, get_settings
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(
    session: AsyncSession,
) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the user for an MCP tool invocation.

    Picks the right auth path based on `settings.mcp_oauth_enabled`.
    Currently both branches use the X-Forwarded-User flow — the OAuth
    branch lands in the next task.

    Returns (user, settings). Raises HTTPException on auth failure;
    the FastMCP layer translates these into MCP errors.
    """
    settings = get_settings()
    http_request = get_http_request()
    user = await resolve_user_from_request(http_request, session, settings)
    return user, settings

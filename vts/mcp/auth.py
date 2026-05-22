from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from vts.core.config import Settings, get_settings
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(
    http_request: Request,
    session: AsyncSession,
) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the user for an MCP tool invocation.

    The caller MUST provide an open async session — auth resolution shares it
    with the rest of the tool body (single round-trip to the pool).
    Returns (user, settings). Raises HTTPException on auth failure (401/403);
    the FastMCP layer translates these into MCP errors.
    """
    settings = get_settings()
    user = await resolve_user_from_request(http_request, session, settings)
    return user, settings

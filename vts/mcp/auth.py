from __future__ import annotations

from starlette.requests import Request

from vts.core.config import Settings, get_settings
from vts.db.session import get_db_session_factory
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(http_request: Request) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the user for an MCP tool invocation.

    Returns (user, settings). Raises HTTPException on auth failure (401/403);
    the FastMCP layer translates these into MCP errors.
    """
    settings = get_settings()
    session_factory = get_db_session_factory()
    async with session_factory() as session:
        user = await resolve_user_from_request(http_request, session, settings)
    return user, settings

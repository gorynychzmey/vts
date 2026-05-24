from __future__ import annotations

from fastmcp.server.dependencies import get_http_request
from sqlalchemy.ext.asyncio import AsyncSession

from vts.core.config import Settings, get_settings
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(
    session: AsyncSession,
) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the calling user for an MCP tool — delegates to the
    single resolve_user_from_request used by REST as well."""
    settings = get_settings()
    request = get_http_request()
    user = await resolve_user_from_request(request, session, settings)
    return user, settings

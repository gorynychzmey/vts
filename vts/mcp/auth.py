from __future__ import annotations

from fastapi import HTTPException
from fastmcp.server.dependencies import get_access_token, get_http_request
from sqlalchemy.ext.asyncio import AsyncSession

from vts.core.config import Settings, get_settings
from vts.db.repo import Repo
from vts.mcp.allowlist import is_email_allowed
from vts.services.auth import AuthenticatedUser, resolve_user_from_request


async def mcp_authenticate(
    session: AsyncSession,
) -> tuple[AuthenticatedUser, Settings]:
    """Resolve the user for an MCP tool invocation.

    If `settings.mcp_oauth_enabled`, identity comes from the FastMCP
    OAuth access token (email claim populated by GoogleProvider).
    Otherwise, falls back to the X-Forwarded-User reverse-proxy flow.

    Returns (user, settings). Raises HTTPException 401 / 403 on failure;
    FastMCP translates these to MCP errors.
    """
    settings = get_settings()
    if settings.mcp_oauth_enabled:
        return await _authenticate_via_oauth(session, settings)
    http_request = get_http_request()
    user = await resolve_user_from_request(http_request, session, settings)
    return user, settings


async def _authenticate_via_oauth(
    session: AsyncSession,
    settings: Settings,
) -> tuple[AuthenticatedUser, Settings]:
    token = get_access_token()
    if token is None:
        raise HTTPException(status_code=401, detail="Missing OAuth access token")
    email = (token.claims or {}).get("email")
    if not email:
        raise HTTPException(status_code=401, detail="OAuth token has no email claim")
    if not is_email_allowed(
        email,
        allowed_emails=settings.mcp_oauth_allowed_emails,
        allowed_domains=settings.mcp_oauth_allowed_domains,
    ):
        raise HTTPException(status_code=403, detail="Email not allowed")

    username = email.strip().lower()
    repo = Repo(session)
    db_user = await repo.get_or_create_user(username)
    await session.commit()
    user = AuthenticatedUser(
        id=str(db_user.id),
        username=db_user.username,
        requested_by=username,
        is_admin=settings.is_admin(username),
        acting_as=username,
    )
    return user, settings

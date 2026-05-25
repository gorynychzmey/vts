from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastmcp.server.dependencies import get_access_token
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from vts.core.config import Settings, get_settings
from vts.db.repo import Repo
from vts.db.session import get_db_session


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    username: str
    requested_by: str
    is_admin: bool
    acting_as: str


async def resolve_user_from_request(
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> AuthenticatedUser:
    """Single auth entrypoint. Picks one of three branches:

    1. oauth_enabled=False → trust X-Forwarded-User (dev only, no proxy check).
    2. oauth_enabled=True + Authorization: Bearer → FastMCP access token claims.
    3. oauth_enabled=True + signed session cookie → session['email'].

    Allow-list applies to the bearer branch only (the session branch is
    already gated by /auth/callback at login time).
    """
    if not settings.oauth_enabled:
        email = request.headers.get("x-forwarded-user", "").strip()
        if not email:
            raise HTTPException(status_code=401, detail="Missing X-Forwarded-User (dev mode)")
        return await _materialize_user(email, request, session, settings)

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = get_access_token()
        if token is None:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        email = (token.claims or {}).get("email", "")
        if not email:
            raise HTTPException(status_code=401, detail="Bearer token has no email claim")
        from vts.mcp.allowlist import is_email_allowed  # local to avoid circular import
        if not is_email_allowed(
            email,
            allowed_emails=settings.oauth_allowed_emails,
            allowed_domains=settings.oauth_allowed_domains,
        ):
            raise HTTPException(status_code=403, detail="Email not allowed")
        return await _materialize_user(email, request, session, settings)

    # Browser path: session cookie via Starlette SessionMiddleware.
    starlette_session = getattr(request, "session", None) or {}
    email = (starlette_session.get("email") or "").strip() if isinstance(starlette_session, dict) else ""
    if email:
        # Re-check the allow-list on EVERY request (vts-jo2 / audit Finding 1).
        # /auth/callback gates only at login time; without this re-check,
        # removing a user from oauth_allowed_emails / oauth_allowed_domains
        # would not take effect until the cookie's 30-day max-age elapsed.
        from vts.mcp.allowlist import is_email_allowed  # local to avoid circular import
        if not is_email_allowed(
            email,
            allowed_emails=settings.oauth_allowed_emails,
            allowed_domains=settings.oauth_allowed_domains,
        ):
            raise HTTPException(status_code=403, detail="Email no longer allowed")
        return await _materialize_user(email, request, session, settings)

    raise HTTPException(status_code=401, detail="Authentication required")


async def _materialize_user(
    email: str,
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> AuthenticatedUser:
    requested_by = email.strip().lower()
    is_admin = settings.is_admin(requested_by)
    acting_as = requested_by
    requested_as = request.query_params.get("as_user")
    if requested_as:
        candidate = requested_as.strip()
        if not candidate:
            raise HTTPException(status_code=400, detail="Empty as_user value")
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admin can switch user context")
        acting_as = candidate

    repo = Repo(session)
    if requested_as:
        user = await repo.get_user_by_username(acting_as)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target user not found for admin switch",
            )
    else:
        user = await repo.get_or_create_user(acting_as)
    await session.commit()
    return AuthenticatedUser(
        id=str(user.id),
        username=user.username,
        requested_by=requested_by,
        is_admin=is_admin,
        acting_as=acting_as,
    )


# require_user — kept as a FastAPI dependency for OpenAPI docs.
async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """FastAPI Depends wrapper kept for OpenAPI; delegates to resolve_user_from_request."""
    return await resolve_user_from_request(request, session, settings)

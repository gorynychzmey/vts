from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    redis: Any | None = None,
) -> AuthenticatedUser:
    """Single auth entrypoint. Picks one of three branches:

    1. oauth_enabled=False → trust X-Forwarded-User (dev only, no proxy check).
    2. oauth_enabled=True + Authorization: Bearer → FastMCP access token claims.
    3. oauth_enabled=True + signed session cookie → server-side session
       record in Redis (vts-pa9), falling back to legacy email-only cookies.
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
    if not isinstance(starlette_session, dict):
        starlette_session = {}

    email: str | None = None
    sid = (starlette_session.get("sid") or "").strip() if starlette_session else ""
    if sid and redis is not None:
        # vts-pa9: cookie carries opaque sid; email lives in Redis. A
        # missing record means /auth/logout deleted it or it expired —
        # in both cases force re-login.
        from vts.services import session_store
        record = await session_store.lookup(redis, sid)
        if record is None:
            raise HTTPException(status_code=401, detail="Session expired or revoked")
        email = record.email
    else:
        # Legacy fallback for cookies issued before vts-pa9 (or test
        # contexts that don't supply Redis). Safe because vts-jo2's
        # allow-list re-check still applies below.
        legacy_email = (starlette_session.get("email") or "").strip()
        if legacy_email:
            email = legacy_email

    if email:
        # vts-jo2: per-request allow-list re-check so an operator removing
        # someone from oauth_allowed_* takes effect on the next request.
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
        # vts-9kk: stored usernames are .strip().lower() emails (set by
        # /auth/callback). Normalise the admin's switch input the same
        # way so '?as_user=Alice@Example.com' matches the existing row.
        candidate = requested_as.strip().lower()
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
    redis = getattr(request.app.state, "redis", None) if hasattr(request, "app") else None
    return await resolve_user_from_request(request, session, settings, redis=redis)

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Core auth logic, callable from both FastAPI Depends and FastMCP tools."""
    remote_host = request.client.host if request.client else "127.0.0.1"
    if not settings.is_trusted_proxy(remote_host):
        raise HTTPException(status_code=403, detail="Untrusted proxy source for forwarded auth header")
    x_forwarded_user = request.headers.get("x-forwarded-user")
    if not x_forwarded_user and settings.environment != "prod":
        x_forwarded_user = request.query_params.get("dev_user")
    if not x_forwarded_user:
        raise HTTPException(status_code=401, detail="Missing X-Forwarded-User header")

    requested_by = x_forwarded_user.strip()
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


async def require_user(
    request: Request,
    x_forwarded_user: str | None = Header(default=None, alias="X-Forwarded-User"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    # x_forwarded_user kept as a FastAPI Header param for OpenAPI docs only;
    # the resolver reads it directly from the request.
    _ = x_forwarded_user
    return await resolve_user_from_request(request, session, settings)

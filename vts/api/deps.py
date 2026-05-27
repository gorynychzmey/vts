from __future__ import annotations

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vts.core.config import Settings, get_settings
from vts.db.session import get_db_session
from vts.services.auth import AuthenticatedUser, require_user


def get_settings_dep() -> Settings:
    return get_settings()


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


async def get_session_dep(session: AsyncSession = Depends(get_db_session)) -> AsyncSession:
    return session


async def get_current_user(user: AuthenticatedUser = Depends(require_user)) -> AuthenticatedUser:
    return user


async def get_current_user_session_only(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
) -> AuthenticatedUser:
    """Same as get_current_user, but rejects Authorization: Bearer.

    Used by /api/me/tokens endpoints to prevent an API token from issuing
    or revoking other API tokens (rotation-attack prevention). Token
    management must require a real interactive session.
    """
    from fastapi import HTTPException
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(
            status_code=403,
            detail="API tokens cannot be managed via bearer auth; use a session.",
        )
    return user


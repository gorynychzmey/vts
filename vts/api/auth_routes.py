from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from vts.core.config import Settings, get_settings
from vts.db.repo import Repo
from vts.db.session import get_db_session_factory
from vts.mcp.allowlist import is_email_allowed
from vts.services.web_oauth import build_oauth_client


router = APIRouter()
_oauth_client_cache: dict[str, object] = {}


def _get_oauth(settings: Settings):
    key = f"{settings.oauth_client_id}:{settings.oauth_client_secret}"
    if key not in _oauth_client_cache:
        _oauth_client_cache[key] = build_oauth_client(settings)
    return _oauth_client_cache[key]


def _safe_next(value: str | None) -> str:
    if not value:
        return "/"
    if not value.startswith("/"):
        return "/"
    if value.startswith("//"):
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


@router.get("/auth/login")
async def auth_login(request: Request):
    settings = get_settings()
    if not settings.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth not enabled")
    next_path = _safe_next(request.query_params.get("next"))
    request.session["next_after_login"] = next_path
    oauth = _get_oauth(settings)
    redirect_uri = f"{settings.public_base_url}/auth/callback"
    google = oauth.create_client("google")
    return await google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    settings = get_settings()
    if not settings.oauth_enabled:
        raise HTTPException(status_code=404, detail="OAuth not enabled")
    oauth = _get_oauth(settings)
    google = oauth.create_client("google")
    try:
        token = await google.authorize_access_token(request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {exc}") from exc
    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google did not return an email claim")
    if not is_email_allowed(
        email,
        allowed_emails=settings.oauth_allowed_emails,
        allowed_domains=settings.oauth_allowed_domains,
    ):
        raise HTTPException(status_code=403, detail=f"Email {email} is not allowed for this vts instance")

    session_factory = get_db_session_factory()
    async with session_factory() as db:
        repo = Repo(db)
        await repo.get_or_create_user(email)
        await db.commit()

    request.session["email"] = email
    next_path = _safe_next(request.session.pop("next_after_login", "/"))
    return RedirectResponse(url=next_path, status_code=302)


@router.post("/auth/logout")
async def auth_logout(request: Request):
    request.session.pop("email", None)
    return Response(status_code=204)

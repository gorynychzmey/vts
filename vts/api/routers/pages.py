"""Static pages and PWA plumbing: the SPA shell, share target, privacy, health.

Everything a browser asks for that is not the JSON API: the index page (which
redirects to login when OAuth is on), the web-app manifest and service worker,
the PWA share target, the privacy notice, and the `/healthz` probe.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged; `settings` came from the enclosing closure and
is now taken via `Depends(get_settings_dep)` or `get_settings()`, which is
`lru_cache`d and hands back the same object.

Assets are served from `vts.api.main.STATIC_DIR` rather than a path computed
here: this module sits one directory deeper than `main`, so a copied
`parents[1]` would resolve to `vts/api/static` instead of `vts/static`.

No `tags=` on the router: `_install_custom_openapi()` in `vts.api.main`
derives the OpenAPI tag from the URL prefix, and an explicit tag overrides it.
"""

from __future__ import annotations

import logging
import urllib.parse
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from vts import __version__
from vts.api._helpers.pages_assets import NO_CACHE_HEADERS, STATIC_DIR, _render_privacy_page
from vts.api.deps import get_settings_dep
from vts.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    if get_settings().oauth_enabled:
        session_data = getattr(request, "session", None) or {}
        if not isinstance(session_data, dict):
            session_data = {}
        # vts-pa9: prefer sid (current cookie shape); fall back to
        # legacy email (cookies issued before vts-pa9). Either presence
        # means the user has a session — the resolver will validate it
        # on the next authenticated call.
        has_session = bool(
            (session_data.get("sid") or "").strip()
            or (session_data.get("email") or "").strip()
        )
        if not has_session:
            import urllib.parse
            return RedirectResponse(
                url=f"/auth/login?next={urllib.parse.quote(request.url.path, safe='')}",
                status_code=302,
            )
    template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    content = template.replace("__VTS_VERSION__", __version__)
    return HTMLResponse(content=content, headers=NO_CACHE_HEADERS)

@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(
        path=str(STATIC_DIR / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )

@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    # Serve service worker from root so its scope covers the whole app.
    return FileResponse(
        path=str(STATIC_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
    )

@router.post("/share", include_in_schema=False)
async def share_target_post() -> RedirectResponse:
    # POST /share is normally intercepted by the service worker, which
    # stashes any shared file and redirects the client. If the SW isn't
    # active yet (first launch after install), fall back to the root so
    # the user at least lands in the app.
    return RedirectResponse(url="/?share_error=sw_not_ready", status_code=303)

@router.get("/share", include_in_schema=False)
async def share_target(
    url: str | None = None,
    text: str | None = None,
    title: str | None = None,
) -> RedirectResponse:
    # Android share sheet passes arbitrary payloads. YouTube typically
    # puts the URL into `text`. Forward everything and let the frontend
    # pick the best candidate.
    params: dict[str, str] = {}
    if url:
        params["share_url"] = url
    if text:
        params["share_text"] = text
    if title:
        params["share_title"] = title
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/{query}", status_code=303)

@router.get("/healthz", include_in_schema=False)
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok")

@router.get("/privacy", include_in_schema=False, response_class=HTMLResponse)
async def privacy_policy(
    settings: Settings = Depends(get_settings_dep),
) -> HTMLResponse:
    return HTMLResponse(_render_privacy_page(settings))

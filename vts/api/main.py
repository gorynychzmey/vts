from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import secrets
import signal
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from starlette.middleware.sessions import SessionMiddleware

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from redis.asyncio import Redis
from sqlalchemy import text

from vts import __version__
from vts.api._helpers.pages_assets import STATIC_DIR
from vts.api.deps import get_current_user, get_settings_dep
from vts.api.schemas import TextSliceOut
from vts.core.config import Settings
from vts.core.logging import configure_logging
from vts.db.models import Task























































_PRIVACY_TEMPLATE_HTML: str | None = None






















#: A year, the maximum any cache should honour, plus `immutable` so browsers
#: skip even the revalidation round-trip.
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


class _ImmutableStaticFiles(StaticFiles):
    """Serve bundled assets with a long, immutable Cache-Control.

    Safe because every asset the page references is version-addressed: the
    JS/CSS tags in index.html carry `?v=<app version>` (substituted in
    vts/api/routers/pages.py), and fonts change name when they change content.
    A release therefore produces new URLs rather than stale hits.

    Without this the assets still cache, but only via ETag revalidation — the
    browser pays a conditional request per asset per load just to be told 304.
    index.html itself is NOT served from here; it is rendered by the pages
    router with no-store, which is what lets a new version be picked up at all.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", _IMMUTABLE_CACHE_CONTROL)
        return response


def _resolve_session_secret(*, env_secret: str | None, secret_file: Path) -> str:
    """Resolve the SessionMiddleware HMAC key.

    Priority:
      1. VTS_SESSION_SECRET env (explicit / HA / multi-host deployments).
      2. Contents of secret_file. Auto-created on first start so a fresh
         self-hosted install does not require manual key generation.

    On first start the file is written with mode 0600 via O_EXCL so
    parallel uvicorn workers cannot both write — the loser of the race
    catches FileExistsError and reads what the winner wrote.
    """
    if env_secret:
        return env_secret

    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(secret_file), flags, 0o600)
    except FileExistsError:
        # Another worker won the race; read its value.
        return secret_file.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_secret)
    except Exception:
        # On any write failure, remove the half-written file so the next
        # start retries cleanly rather than reading an empty secret.
        try:
            secret_file.unlink()
        except OSError:
            pass
        raise
    logging.getLogger(__name__).info(
        "generated new session secret at %s", secret_file
    )
    return new_secret


def _downgrade_to_openapi_30(node: Any) -> Any:
    """Convert OpenAPI 3.1 nullable forms into 3.0-compatible
    `{type: ..., nullable: true}` recursively.

    ChatGPT Custom Actions advertise support for OpenAPI 3.1.x but their
    response-validation pipeline chokes on the 3.1 nullable form
    `anyOf: [{type: "string"}, {type: "null"}]` — clients see
    `ClientResponseError` even though our server returned 200 OK. The
    fix is to rewrite those constructs to the older
    `{type: "string", nullable: true}` shape and downgrade the spec
    version string to 3.0.3.

    Pydantic v2 emits the 3.1 form unconditionally, so we transform the
    spec after FastAPI builds it.
    """
    if isinstance(node, dict):
        # Case: anyOf/oneOf containing a `{type: "null"}` sibling.
        for key in ("anyOf", "oneOf"):
            variants = node.get(key)
            if isinstance(variants, list):
                null_variants = [
                    v for v in variants
                    if isinstance(v, dict) and v.get("type") == "null"
                ]
                non_null = [
                    v for v in variants
                    if not (isinstance(v, dict) and v.get("type") == "null")
                ]
                if null_variants and non_null:
                    # If exactly one non-null branch remains, inline it and
                    # mark nullable. Otherwise wrap the surviving branches
                    # back into the anyOf/oneOf with a nullable sibling
                    # (rare in our spec).
                    if len(non_null) == 1:
                        # Drop the anyOf wrapper, merge its single branch
                        # into the parent, and set nullable on the result.
                        node.pop(key)
                        for k, v in non_null[0].items():
                            node.setdefault(k, v)
                        node["nullable"] = True
                    else:
                        node[key] = non_null
                        node["nullable"] = True
        # Case: 3.1 union "type": ["string", "null"]
        t = node.get("type")
        if isinstance(t, list):
            non_null_types = [x for x in t if x != "null"]
            if len(non_null_types) == 1:
                node["type"] = non_null_types[0]
                if "null" in t:
                    node["nullable"] = True
            elif "null" in t:
                node["type"] = non_null_types
                node["nullable"] = True
        # Recurse.
        for v in node.values():
            _downgrade_to_openapi_30(v)
    elif isinstance(node, list):
        for item in node:
            _downgrade_to_openapi_30(item)
    return node


def _install_custom_openapi(app: FastAPI, settings: Settings) -> None:
    """Override app.openapi() so the generated spec is suitable for
    external clients (e.g. GPT Custom Actions, curl/Postman).

    On top of FastAPI's auto-generated spec we add:
      - `servers` with the deployment's public base URL (if configured)
      - `securitySchemes.ApiToken` (HTTP Bearer) + global default security
      - Per-path tags grouped by URL prefix (tasks, meta, admin)
      - Downgrade 3.1 nullable form to 3.0-compat for client compatibility
    """
    from fastapi.openapi.utils import get_openapi

    def _tag_for_path(path: str) -> str:
        if path.startswith("/api/tasks"):
            return "tasks"
        if path.startswith("/api/admin"):
            return "admin"
        return "meta"

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        if settings.public_base_url:
            schema["servers"] = [{"url": settings.public_base_url.rstrip("/")}]
        # Schemas referenced only via responses[...]['content']['$ref']
        # don't get auto-collected by FastAPI; inject them explicitly so
        # OpenAPI consumers can resolve the $ref.
        components = schema.setdefault("components", {})
        registered_schemas = components.setdefault("schemas", {})
        for extra_model in (TextSliceOut,):
            name = extra_model.__name__
            if name not in registered_schemas:
                registered_schemas[name] = extra_model.model_json_schema(
                    ref_template="#/components/schemas/{model}"
                )
        components["securitySchemes"] = {
            "ApiToken": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Personal API token issued from the VTS UI "
                    "(header → key icon → Create token). Format: `vts_<43 chars>`. "
                    "Browser session cookies also work for the same endpoints but "
                    "are out of scope for external clients."
                ),
            }
        }
        # Apply globally; unauthenticated endpoints opt out individually below.
        schema["security"] = [{"ApiToken": []}]
        for path, methods in schema.get("paths", {}).items():
            tag = _tag_for_path(path)
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                op.setdefault("tags", [tag])
        # Endpoints that must NOT require auth in the spec.
        for path in ("/api/version", "/api/status-config", "/healthz"):
            for op in schema.get("paths", {}).get(path, {}).values():
                if isinstance(op, dict):
                    op["security"] = []
        # Rewrite the 3.1 nullable form `anyOf: [..., {type: null}]` to the
        # widely-supported `nullable: true` extension. ChatGPT Custom Actions
        # validator chokes on the former even though it parses fine
        # elsewhere; the latter is accepted by both 3.0.x and 3.1.x clients
        # in practice. We keep the 3.1.0 header so ChatGPT's "must be
        # 3.1.0/3.1.1" check passes, even though `nullable` is technically a
        # 3.0 leftover — most validators (incl. ChatGPT, Swagger UI, Redoc)
        # honour it regardless of declared version.
        _downgrade_to_openapi_30(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]










def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings_dep()

    if settings.oauth_enabled:
        if not settings.oauth_client_secret:
            raise RuntimeError(
                "oauth_enabled=True but oauth_client_secret is missing — "
                "set VTS_OAUTH_CLIENT_SECRET"
            )
        session_secret = _resolve_session_secret(
            env_secret=settings.session_secret,
            secret_file=settings.session_secret_file,
        )

    # Build the MCP sub-app eagerly so we can chain its lifespan into ours;
    # FastAPI does not run lifespans of mounted sub-apps, and the FastMCP
    # streamable-http transport initialises its session manager only via
    # that lifespan.
    mcp_app = None
    mcp_oauth_routes: list = []
    if settings.mcp_enabled:
        from vts.mcp import build_mcp_app_with_wellknown
        mcp_app, mcp_oauth_routes = build_mcp_app_with_wellknown(settings.mcp_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Say whether this deployment runs the prompts we shipped. An operator
        # can mount a prompts directory over the image's own to override one
        # for the whole service, and that is meant to outlive releases — so a
        # newly released wording silently does not reach them (vts-2a0w).
        from vts.services.system_prompt import log_prompt_overrides

        log_prompt_overrides(settings.prompts_dir)
        # Watched by long-lived streams (/api/events) so they can end
        # themselves. Without it uvicorn's graceful shutdown waits on SSE
        # clients that never disconnect, so the container only died once
        # --timeout-graceful-shutdown expired and SIGKILL arrived (vts-9er).
        app.state.shutting_down = asyncio.Event()
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=False)
        # Setting the flag here in the `finally` is too late to be useful on
        # its own: uvicorn waits for open connections BEFORE running the
        # lifespan, so an idle SSE stream held the stop for the whole
        # --timeout-graceful-shutdown (measured twice on prod: 15s).
        #
        #   connection.shutdown() for each connection
        #   await asyncio.wait_for(_wait_tasks_to_complete(), timeout=...)  <- waits
        #   await self.lifespan.shutdown()                                  <- too late
        #
        # uvicorn installs its own SIGTERM/SIGINT handler with plain
        # signal.signal (server.py:319) and that handler only flips
        # `should_exit`. So we chain ours in front of it: ours fires at signal
        # delivery, before shutdown() is entered, and the streams end
        # themselves while uvicorn is still closing listeners. Measured with
        # one live SSE client: 15.19s without this, 0.19s with it.
        loop = asyncio.get_running_loop()
        previous: dict[int, Any] = {}

        def _note_shutdown(sig: int, frame: Any) -> None:
            # Runs in the signal context, so only schedule work on the loop.
            loop.call_soon_threadsafe(app.state.shutting_down.set)
            chained = previous.get(sig)
            if callable(chained):
                chained(sig, frame)

        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _note_shutdown)
            except ValueError:
                # Not the main thread (tests, embedded runs): the flag still
                # gets set by the lifespan below, just as late as before.
                previous.pop(sig, None)
            else:
                installed.append(sig)
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            app.state.shutting_down.set()
            for sig in installed:
                # Hand the signal back, so uvicorn's own restore in
                # capture_signals() puts back what was there before us.
                with suppress(ValueError):
                    signal.signal(sig, previous[sig])
            await app.state.redis.aclose()

    app = FastAPI(
        title="vts",
        version=__version__,
        description=(
            "Self-hosted video transcription and summarisation API. "
            "Authenticate with a personal API token from the VTS web UI "
            "(header → key icon → Create token). "
            "Send it as `Authorization: Bearer vts_…`. "
            "See https://github.com/gorynychzmey/vts/blob/main/docs/AUTH.md "
            "for the full auth model and "
            "https://github.com/gorynychzmey/vts/blob/main/docs/API.md "
            "for programmatic-access details (incl. GPT Custom Actions)."
        ),
        lifespan=lifespan,
    )
    _install_custom_openapi(app, settings)

    if settings.oauth_enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            session_cookie="vts_session",
            https_only=True,
            same_site="lax",
            max_age=settings.session_max_age_days * 86_400,
        )

    if settings.oauth_enabled:
        from vts.api.auth_routes import router as auth_router
        app.include_router(auth_router)

    # FastMCP's OAuth routes (/.well-known/oauth-*, /authorize, /token,
    # /register, /consent, /<mcp_path>/auth/callback) all live at host
    # root per RFC 8414/9728. Mount them on the parent FastAPI BEFORE the
    # MCP sub-app so they win path matching.
    for route in mcp_oauth_routes:
        app.router.routes.append(route)

    app.mount("/static", _ImmutableStaticFiles(directory=STATIC_DIR), name="static")
    if mcp_app is not None:
        app.mount(settings.mcp_path, mcp_app)

    # Domain routers. Imported here rather than at module scope: they reach
    # back into this module for helpers that have not moved yet, so a
    # top-level import would be a cycle (docs/plans/main-py-split.md).
    #
    # Order is the order FastAPI matches in. It matters where a literal path
    # competes with a parameterised one, so keep related prefixes together and
    # do not reshuffle these lines casually.
    from vts.api.routers.artifacts import router as artifacts_router
    from vts.api.routers.delivery import router as delivery_router
    from vts.api.routers.meta import router as meta_router
    from vts.api.routers.pages import router as pages_router
    from vts.api.routers.recordings import router as recordings_router
    from vts.api.routers.speakers import router as speakers_router
    from vts.api.routers.tasks import router as tasks_router
    from vts.api.routers.uploads import router as uploads_router

    for domain_router in (
        pages_router,
        meta_router,
        delivery_router,
        tasks_router,
        uploads_router,
        artifacts_router,
        speakers_router,
        recordings_router,
    ):
        app.include_router(domain_router)


    return app


app = create_app()

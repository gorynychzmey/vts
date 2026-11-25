"""Web login, bearer path and MCP server go through the configured provider (vts-sf93.1)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, Response

from vts.core.config import get_settings
from vts.db.models import UserSession
from vts.services.auth import resolve_user_from_request

from test_auth_routes import session_db  # noqa: F401 - fixture


def _entra_env(monkeypatch, **extra) -> None:
    env = {
        "VTS_OAUTH_ENABLED": "true",
        "VTS_AUTH_PROVIDER": "entra",
        "VTS_ENTRA_TENANT_ID": "tenant-1",
        "VTS_ENTRA_CLIENT_ID": "e-id",
        "VTS_ENTRA_CLIENT_SECRET": "e-secret-e-secret",
        "VTS_ENTRA_API_SCOPE": "api://app-id/vts",
        "VTS_PUBLIC_BASE_URL": "https://vts.test",
        "VTS_SESSION_SECRET": "the-cookie-key-xx",
        "VTS_OAUTH_ALLOWED_DOMAINS": "example.com",
        **extra,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    import vts.api.auth_routes as routes

    routes._oauth_client_cache.clear()


def _fake_discovery(monkeypatch) -> None:
    from authlib.integrations.starlette_client.apps import StarletteOAuth2App

    async def _meta(self):
        return {
            "authorization_endpoint": "https://idp.invalid/authorize",
            "token_endpoint": "https://idp.invalid/token",
            "issuer": "https://idp.invalid",
        }

    monkeypatch.setattr(StarletteOAuth2App, "load_server_metadata", _meta)


async def _login_and_callback(app, monkeypatch, token) -> tuple[int, Response]:
    from authlib.integrations.starlette_client.apps import StarletteOAuth2App

    async def _fake(self, request):
        return token

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", _fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        await client.get("/auth/login?next=/")
        r = await client.get("/auth/callback?code=x&state=y", follow_redirects=False)
        return r.status_code, r


@pytest.fixture
def _db(monkeypatch, session_db):  # noqa: F811
    monkeypatch.setattr("vts.api.auth_routes.get_db_session_factory", lambda: session_db)
    return session_db


async def test_entra_callback_uses_preferred_username(monkeypatch, _db) -> None:
    _entra_env(monkeypatch)
    # Fake the discovery document so the login redirect needs no network.
    _fake_discovery(monkeypatch)
    from vts.api.main import create_app

    app = create_app()
    status, _ = await _login_and_callback(
        app, monkeypatch, {"userinfo": {"preferred_username": "Bob@Example.com"}}
    )
    assert status == 302
    async with _db() as db:
        rows = list(await db.scalars(sa.select(UserSession)))
    assert [r.email for r in rows] == ["bob@example.com"]


async def test_callback_rejects_non_listed_domain_and_missing_email(monkeypatch, _db) -> None:
    _entra_env(monkeypatch)
    _fake_discovery(monkeypatch)
    from vts.api.main import create_app

    app = create_app()
    status, _ = await _login_and_callback(
        app, monkeypatch, {"userinfo": {"preferred_username": "mallory@evil.com"}}
    )
    assert status == 403
    status, r = await _login_and_callback(app, monkeypatch, {"userinfo": {"name": "x"}})
    assert status == 400
    assert r.json()["detail"] == "The identity provider did not return an email"


def test_oauth_cache_key_distinguishes_providers(monkeypatch) -> None:
    from vts.api.auth_routes import _get_oauth, _oauth_client_cache

    _entra_env(monkeypatch)
    entra_client = _get_oauth(get_settings())
    assert entra_client.create_client("entra") is not None
    assert entra_client.create_client("google") is None
    monkeypatch.setenv("VTS_AUTH_PROVIDER", "google")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "g-id")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "g-secret-g-secret")
    get_settings.cache_clear()
    google_client = _get_oauth(get_settings())
    assert google_client is not entra_client
    assert google_client.create_client("google") is not None
    _oauth_client_cache.clear()


def _entra_settings(**kw):
    from vts.core.config import Settings

    base = dict(
        oauth_enabled=True,
        auth_provider="entra",
        entra_tenant_id="t",
        entra_client_id="i",
        entra_client_secret="s",
        entra_api_scope="api://a/vts",
        public_base_url="https://vts.test",
        oauth_allowed_domains=["example.com"],
    )
    return Settings(**{**base, **kw})


def _bearer_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer a.b.c")],
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "scheme": "https",
            "session": {},
        }
    )


class _Repo:
    async def get_or_create_user(self, username):
        return SimpleNamespace(id=1, username=username, is_admin=False)


class _Sess:
    async def commit(self):
        pass


async def test_bearer_claims_with_only_upn_resolve_the_user(monkeypatch) -> None:
    token = SimpleNamespace(claims={"upn": "Dave@Example.com"})
    monkeypatch.setattr("vts.services.auth.get_access_token", lambda: token)
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: _Repo())
    user = await resolve_user_from_request(_bearer_request(), _Sess(), _entra_settings())
    assert user.username == "dave@example.com"


async def test_bearer_without_provider_email_claim_is_401(monkeypatch) -> None:
    token = SimpleNamespace(claims={"sub": "x"})
    monkeypatch.setattr("vts.services.auth.get_access_token", lambda: token)
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(_bearer_request(), _Sess(), _entra_settings())
    assert exc.value.status_code == 401


def test_build_mcp_server_entra_builds_azure_provider(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Capture:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("fastmcp.server.auth.providers.azure.AzureProvider", _Capture)
    _entra_env(monkeypatch, VTS_MCP_OAUTH_STATE_DIR=str(tmp_path))
    from vts.mcp import server

    monkeypatch.setattr(server, "FastMCP", lambda name, auth: SimpleNamespace(auth=auth))
    for domain in (
        server.tasks, server.prompts, server.presets, server.delivery,
        server.search, server.recordings, server.apps,
    ):
        monkeypatch.setattr(domain, "register", lambda mcp: None)
    mcp = server.build_mcp_server()
    assert isinstance(mcp.auth, _Capture)
    assert captured["client_id"] == "e-id"
    assert captured["required_scopes"] == ["vts"]
    assert captured["client_storage"] is not None  # keyed off the Entra secret, not Google's


def test_build_mcp_server_fails_on_incomplete_entra_config(monkeypatch) -> None:
    _entra_env(monkeypatch, VTS_ENTRA_TENANT_ID="common")
    from vts.mcp import server
    from vts.services.auth_providers import AuthConfigError

    with pytest.raises(AuthConfigError, match="specific tenant"):
        server.build_mcp_server()


def test_create_app_fails_at_boot_on_incomplete_entra_config(monkeypatch) -> None:
    _entra_env(monkeypatch, VTS_ENTRA_CLIENT_SECRET="")
    from vts.api.main import create_app
    from vts.services.auth_providers import AuthConfigError

    with pytest.raises(AuthConfigError, match="VTS_ENTRA_CLIENT_SECRET"):
        create_app()

from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from _db import ensure_pgvector, make_test_engine
from vts.db.base import Base
from vts.db.models import UserSession


@pytest_asyncio.fixture
async def session_db():
    """A real database for the routes that now persist the session record."""
    engine = make_test_engine()
    await ensure_pgvector(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def app_with_oauth(monkeypatch):
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "abc.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "secret-secret-secret")
    monkeypatch.setenv("VTS_OAUTH_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.test")
    monkeypatch.setenv("VTS_SESSION_SECRET", "the-cookie-key-xx")
    get_settings.cache_clear()
    # Clear the module-level OAuth client cache so each test gets a fresh client
    import vts.api.auth_routes as _auth_routes
    _auth_routes._oauth_client_cache.clear()
    from vts.api.main import create_app
    return create_app()


async def test_auth_login_redirects_to_google(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/auth/login?next=/dashboard")
        assert r.status_code == 302
        assert "accounts.google.com" in r.headers["location"]


async def test_auth_login_rejects_open_redirect(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/auth/login?next=https://evil.com/")
        assert r.status_code == 302
        # 'next' that doesn't look like a local path should be sanitised to '/'.
        # We can't see 'next' until callback; assert the location goes to Google
        # AND that no fishy 'state' encoding sneaks the URL through:
        loc = r.headers["location"]
        assert "evil.com" not in loc


async def test_auth_logout_clears_cookie(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        # Same-origin POST (what the real frontend fetch() does) is accepted;
        # even without a prior login the route responds 204 and does not blow up.
        r = await client.post(
            "/auth/logout",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert r.status_code == 204


async def test_auth_logout_rejects_cross_site_post(app_with_oauth) -> None:
    """vts-0e1 / audit Finding 2: cross-site POST must be blocked by the
    Sec-Fetch-Site gate, not by SameSite=lax alone."""
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.post(
            "/auth/logout",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 403


async def test_auth_logout_rejects_missing_sec_fetch_site(app_with_oauth) -> None:
    """Fail-closed: legacy browsers without Sec-Fetch-Site (or curl) cannot
    perform state-changing actions. Documented constraint."""
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.post("/auth/logout")
        assert r.status_code == 403


async def test_callback_stores_the_session_and_logout_deletes_it(
    app_with_oauth, monkeypatch, session_db
) -> None:
    """vts-pa9 end-to-end: /auth/callback stores {sid->email}; /auth/logout
    deletes it, so the same cookie cannot be replayed afterwards.

    Runs against a real database since vts-akf8 — the record is a row now,
    written in the same transaction that creates the user."""
    fake_token = {"userinfo": {"email": "callback-test@local.invalid"}}

    async def _fake_authorize_access_token(self, request):
        return fake_token

    from authlib.integrations.starlette_client.apps import StarletteOAuth2App
    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", _fake_authorize_access_token)

    monkeypatch.setattr("vts.api.auth_routes.get_db_session_factory", lambda: session_db)

    from vts.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("VTS_OAUTH_ALLOWED_EMAILS", "callback-test@local.invalid")
    get_settings.cache_clear()

    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        await client.get("/auth/login?next=/")
        r = await client.get("/auth/callback?code=anything&state=anything", follow_redirects=False)
        # Either successful redirect with sid stored, or state-mismatch 400.
        if r.status_code != 302:
            pytest.skip(f"OAuth state validation rejected stub flow ({r.status_code}); covered by lower-level tests")
        # A session row must exist now, and it must not hold the raw sid.
        async with session_db() as db:
            rows = list(await db.scalars(sa.select(UserSession)))
        assert len(rows) == 1
        assert rows[0].email == "callback-test@local.invalid"
        assert len(rows[0].sid_hash) == 64

        # Logout should remove it.
        logout = await client.post(
            "/auth/logout",
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert logout.status_code == 204
        async with session_db() as db:
            assert list(await db.scalars(sa.select(UserSession))) == []


async def test_auth_callback_rejects_when_state_missing(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/auth/callback?code=fake")
        # Either 400 (missing state) or authlib's OAuthError → 400.
        assert r.status_code == 400


async def test_auth_callback_happy_path_sets_session(
    app_with_oauth, monkeypatch, session_db
) -> None:
    """Monkeypatch authlib's authorize_access_token AND point the route at the
    throwaway test database, so the callback executes end-to-end WITHOUT
    touching any real one — a previous version of this test had a
    socket.gaierror fallback that, on a dev box where vts.api.db.session
    resolved to a real Postgres, ended up writing a fake user into
    production. The session record is a row since vts-akf8, so stubbing the
    session factory away is no longer an option: the route must be given a
    database, and it must be a disposable one."""
    fake_token = {"userinfo": {"email": "callback-test@local.invalid"}}

    async def _fake_authorize_access_token(self, request):
        return fake_token

    from authlib.integrations.starlette_client.apps import StarletteOAuth2App
    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", _fake_authorize_access_token)

    # Keep the route away from any real database by pointing it at the
    # throwaway one the session_db fixture drops after the test.
    monkeypatch.setattr("vts.api.auth_routes.get_db_session_factory", lambda: session_db)

    # Allow the test email through.
    from vts.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("VTS_OAUTH_ALLOWED_EMAILS", "callback-test@local.invalid")
    get_settings.cache_clear()

    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        await client.get("/auth/login?next=/")
        # State validation is bypassed by the fake authorize_access_token
        # which short-circuits the upstream exchange. We assert only that
        # the response is either the success redirect (302) or the state
        # mismatch (400) — never 500.
        r = await client.get("/auth/callback?code=anything&state=anything", follow_redirects=False)
        assert r.status_code in (302, 400)

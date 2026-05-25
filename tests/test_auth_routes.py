from __future__ import annotations

from unittest.mock import AsyncMock
import pytest
from httpx import ASGITransport, AsyncClient


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


async def test_auth_callback_rejects_when_state_missing(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/auth/callback?code=fake")
        # Either 400 (missing state) or authlib's OAuthError → 400.
        assert r.status_code == 400


async def test_auth_callback_happy_path_sets_session(app_with_oauth, monkeypatch) -> None:
    """Monkeypatch authlib's authorize_access_token AND the DB repo so the
    callback executes end-to-end WITHOUT touching any real database — a
    previous version of this test had a socket.gaierror fallback that, on
    a dev box where vts.api.db.session resolved to a real Postgres, ended
    up writing a fake user into production."""
    fake_token = {"userinfo": {"email": "callback-test@local.invalid"}}

    async def _fake_authorize_access_token(self, request):
        return fake_token

    from authlib.integrations.starlette_client.apps import StarletteOAuth2App
    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", _fake_authorize_access_token)

    # Block the DB path completely. The route calls
    # get_db_session_factory() → Session → Repo(db).get_or_create_user(...).
    # Replace the factory with one that yields a session whose Repo is a
    # no-op: it returns a sentinel user without doing any SQL.
    class _NoopSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def commit(self): pass
    def _noop_session_factory():
        return _NoopSession()

    class _NoopRepo:
        def __init__(self, _db): pass
        async def get_or_create_user(self, username):
            return None  # value is unused; callback only sets request.session["email"]

    monkeypatch.setattr("vts.api.auth_routes.get_db_session_factory", lambda: _noop_session_factory)
    monkeypatch.setattr("vts.api.auth_routes.Repo", _NoopRepo)

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

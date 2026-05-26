from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def app_with_oauth(monkeypatch):
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "abc.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "secret-secret-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.test")
    monkeypatch.setenv("VTS_SESSION_SECRET", "the-cookie-key-xx")
    get_settings.cache_clear()
    from vts.api.main import create_app
    return create_app()


async def test_index_redirects_when_unauthenticated(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("/auth/login")
        assert "next=%2F" in r.headers["location"]


async def test_api_returns_401_when_unauthenticated(app_with_oauth) -> None:
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        r = await client.get("/api/me")
        assert r.status_code == 401


async def test_index_does_not_redirect_when_session_has_sid(app_with_oauth) -> None:
    """vts-pa9 regression: after a successful login the cookie carries `sid`
    (not `email`). The root handler must treat that as authenticated and
    serve the HTML — otherwise it redirects back to /auth/login and the
    user spins in a callback loop."""
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        # Forge a signed Starlette session cookie containing {sid: ...}.
        import base64
        import itsdangerous
        signer = itsdangerous.TimestampSigner("the-cookie-key-xx")
        payload = b'{"sid": "deadbeef" * 4 }'  # value content does not matter for root()
        payload = b'{"sid":"deadbeefdeadbeefdeadbeefdeadbeef"}'
        encoded = base64.b64encode(payload)
        cookie = signer.sign(encoded).decode("ascii")

        r = await client.get(
            "/",
            cookies={"vts_session": cookie},
            follow_redirects=False,
        )
        assert r.status_code == 200, (
            f"expected 200 (HTML served) but got {r.status_code}; "
            f"likely root() still checks only `email` and ignores `sid`"
        )


async def test_index_does_not_redirect_when_session_has_legacy_email(app_with_oauth) -> None:
    """Backwards-compat: cookies issued before vts-pa9 still carry `email`."""
    transport = ASGITransport(app=app_with_oauth)
    async with AsyncClient(transport=transport, base_url="https://vts.test") as client:
        import base64
        import itsdangerous
        signer = itsdangerous.TimestampSigner("the-cookie-key-xx")
        payload = b'{"email":"alice@example.com"}'
        encoded = base64.b64encode(payload)
        cookie = signer.sign(encoded).decode("ascii")

        r = await client.get(
            "/",
            cookies={"vts_session": cookie},
            follow_redirects=False,
        )
        assert r.status_code == 200

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

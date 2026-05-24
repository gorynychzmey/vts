from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vts.mcp.auth import mcp_authenticate


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_settings_oauth(monkeypatch):
    """Replace get_settings() so oauth_enabled=True."""
    from vts.core.config import Settings

    s = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["vostrikov.de"],
        oauth_allowed_emails=[],
    )
    monkeypatch.setattr("vts.mcp.auth.get_settings", lambda: s)
    return s


async def test_mcp_authenticate_delegates_to_resolve_user(monkeypatch, fake_settings_oauth) -> None:
    """mcp_authenticate must delegate to resolve_user_from_request regardless of oauth flag."""
    sentinel_user = SimpleNamespace(id=str(uuid.uuid4()), username="alice@vostrikov.de", is_admin=False)

    async def _fake_resolve(request, session, settings):
        return sentinel_user

    monkeypatch.setattr("vts.mcp.auth.resolve_user_from_request", _fake_resolve)
    monkeypatch.setattr("vts.mcp.auth.get_http_request", lambda: object())

    session = _FakeSession()
    user, settings = await mcp_authenticate(session)
    assert user is sentinel_user
    assert settings is fake_settings_oauth


async def test_mcp_authenticate_propagates_401(monkeypatch, fake_settings_oauth) -> None:
    """When resolve_user_from_request raises HTTPException 401, mcp_authenticate propagates it."""

    async def _fake_resolve(request, session, settings):
        raise HTTPException(status_code=401, detail="Unauthorized")

    monkeypatch.setattr("vts.mcp.auth.resolve_user_from_request", _fake_resolve)
    monkeypatch.setattr("vts.mcp.auth.get_http_request", lambda: object())

    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await mcp_authenticate(session)
    assert exc.value.status_code == 401


async def test_mcp_authenticate_propagates_403(monkeypatch, fake_settings_oauth) -> None:
    """When resolve_user_from_request raises HTTPException 403, mcp_authenticate propagates it."""

    async def _fake_resolve(request, session, settings):
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr("vts.mcp.auth.resolve_user_from_request", _fake_resolve)
    monkeypatch.setattr("vts.mcp.auth.get_http_request", lambda: object())

    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await mcp_authenticate(session)
    assert exc.value.status_code == 403


async def test_mcp_authenticate_works_when_oauth_disabled(monkeypatch) -> None:
    """When oauth_enabled=False, mcp_authenticate still delegates to resolve_user_from_request."""
    from vts.core.config import Settings

    s = Settings(oauth_enabled=False)
    monkeypatch.setattr("vts.mcp.auth.get_settings", lambda: s)

    sentinel_user = SimpleNamespace(id="u-1", username="legacy", is_admin=False)

    async def _fake_resolve(request, session, settings):
        return sentinel_user

    monkeypatch.setattr("vts.mcp.auth.resolve_user_from_request", _fake_resolve)
    monkeypatch.setattr("vts.mcp.auth.get_http_request", lambda: object())

    session = _FakeSession()
    user, _settings = await mcp_authenticate(session)
    assert user is sentinel_user

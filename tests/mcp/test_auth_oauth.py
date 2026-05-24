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


class _FakeRepo:
    def __init__(self) -> None:
        self.last_arg: str | None = None
        self.user = SimpleNamespace(id=uuid.uuid4(), username="user@vostrikov.de")

    async def get_or_create_user(self, username: str):
        self.last_arg = username
        self.user = SimpleNamespace(id=self.user.id, username=username)
        return self.user


@pytest.fixture
def fake_settings(monkeypatch):
    """Replace get_settings() so OAuth branch fires."""
    from vts.core.config import Settings

    s = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["vostrikov.de"],
        oauth_allowed_emails=[],
    )
    monkeypatch.setattr("vts.mcp.auth.get_settings", lambda: s)
    return s


def _patch_access_token(monkeypatch, token):
    monkeypatch.setattr("vts.mcp.auth.get_access_token", lambda: token)


def _patch_repo(monkeypatch, repo):
    monkeypatch.setattr("vts.mcp.auth.Repo", lambda _session: repo)


async def test_oauth_path_resolves_user_from_email_claim(monkeypatch, fake_settings) -> None:
    token = SimpleNamespace(claims={"email": "alice@vostrikov.de"})
    repo = _FakeRepo()
    _patch_access_token(monkeypatch, token)
    _patch_repo(monkeypatch, repo)
    session = _FakeSession()

    user, settings = await mcp_authenticate(session)
    assert user.username == "alice@vostrikov.de"
    assert repo.last_arg == "alice@vostrikov.de"
    assert session.committed is True
    assert settings is fake_settings


async def test_oauth_path_rejects_when_no_token(monkeypatch, fake_settings) -> None:
    _patch_access_token(monkeypatch, None)
    _patch_repo(monkeypatch, _FakeRepo())
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await mcp_authenticate(session)
    assert exc.value.status_code == 401


async def test_oauth_path_rejects_when_email_claim_missing(monkeypatch, fake_settings) -> None:
    token = SimpleNamespace(claims={"sub": "12345"})  # no email
    _patch_access_token(monkeypatch, token)
    _patch_repo(monkeypatch, _FakeRepo())
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await mcp_authenticate(session)
    assert exc.value.status_code == 401


async def test_oauth_path_rejects_email_not_in_allowlist(monkeypatch, fake_settings) -> None:
    token = SimpleNamespace(claims={"email": "stranger@elsewhere.com"})
    _patch_access_token(monkeypatch, token)
    _patch_repo(monkeypatch, _FakeRepo())
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc:
        await mcp_authenticate(session)
    assert exc.value.status_code == 403


async def test_oauth_path_email_lowercased_before_lookup(monkeypatch, fake_settings) -> None:
    token = SimpleNamespace(claims={"email": "Alice@Vostrikov.de"})
    repo = _FakeRepo()
    _patch_access_token(monkeypatch, token)
    _patch_repo(monkeypatch, repo)
    session = _FakeSession()

    await mcp_authenticate(session)
    assert repo.last_arg == "alice@vostrikov.de"


async def test_legacy_path_still_works_when_oauth_disabled(monkeypatch) -> None:
    """When oauth_enabled=False, fall back to X-Forwarded-User via
    resolve_user_from_request (the existing path; we just smoke that the
    branch is taken)."""
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

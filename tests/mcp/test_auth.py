from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vts.core.config import Settings
from vts.services.auth import resolve_user_from_request


def _make_request(*, headers=None, cookies=None, scheme="https") -> Request:
    headers_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers_list.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "headers": headers_list,
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "scheme": scheme,
        "session": cookies.get("__starlette_session__", {}) if cookies else {},
    }
    return Request(scope)


class _FakeRepo:
    def __init__(self) -> None:
        self.users = {}

    async def get_or_create_user(self, username: str):
        if username not in self.users:
            self.users[username] = SimpleNamespace(id=uuid.uuid4(), username=username)
        return self.users[username]


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


async def test_dev_mode_reads_x_forwarded_user(monkeypatch) -> None:
    """oauth_enabled=False: trust X-Forwarded-User without proxy check."""
    settings = Settings(oauth_enabled=False)
    request = _make_request(headers={"X-Forwarded-User": "alice@example.com"})
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings)
    assert user.username == "alice@example.com"


async def test_dev_mode_rejects_missing_header() -> None:
    settings = Settings(oauth_enabled=False)
    request = _make_request()
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 401


async def test_browser_path_reads_session_cookie_email(monkeypatch) -> None:
    """oauth_enabled=True + request.session has email → user."""
    settings = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
    )
    request = _make_request(cookies={"__starlette_session__": {"email": "bob@example.com"}})
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings)
    assert user.username == "bob@example.com"


async def test_browser_path_rejects_session_email_no_longer_allowed(monkeypatch) -> None:
    """Regression for vts-jo2 (audit Finding 1): if the operator removes a
    user from the allow-list, their existing session cookie must stop
    working on the next request — the session branch must re-check the
    allow-list, not trust the original /auth/callback gate."""
    settings = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
    )
    # Cookie was issued back when alice@elsewhere.com was allowed; now elsewhere.com
    # is no longer in oauth_allowed_domains.
    request = _make_request(cookies={"__starlette_session__": {"email": "alice@elsewhere.com"}})
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 403


async def test_browser_path_rejects_empty_session() -> None:
    settings = Settings(oauth_enabled=True)
    request = _make_request(cookies={"__starlette_session__": {}})
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 401


async def test_bearer_path_reads_access_token_email(monkeypatch) -> None:
    """oauth_enabled=True + Authorization: Bearer ... → user from FastMCP token claims."""
    settings = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
    )
    request = _make_request(headers={"Authorization": "Bearer some.token.value"})

    token = SimpleNamespace(claims={"email": "carol@example.com"})
    monkeypatch.setattr("vts.services.auth.get_access_token", lambda: token)
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings)
    assert user.username == "carol@example.com"


async def test_bearer_path_rejects_disallowed_email(monkeypatch) -> None:
    settings = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
    )
    request = _make_request(headers={"Authorization": "Bearer some.token.value"})
    token = SimpleNamespace(claims={"email": "stranger@elsewhere.com"})
    monkeypatch.setattr("vts.services.auth.get_access_token", lambda: token)
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 403


async def test_oauth_enabled_no_credentials_raises_401(monkeypatch) -> None:
    settings = Settings(oauth_enabled=True)
    request = _make_request()
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 401

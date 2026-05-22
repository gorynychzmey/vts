from __future__ import annotations

import pytest
from starlette.requests import Request
from types import SimpleNamespace

from vts.core.config import Settings
from vts.services.auth import resolve_user_from_request


def _make_request(headers: dict[str, str], client_host: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": (client_host, 12345),
    }
    return Request(scope)


class _FakeRepo:
    def __init__(self) -> None:
        self.users: dict[str, SimpleNamespace] = {}

    async def get_or_create_user(self, username: str) -> SimpleNamespace:
        if username not in self.users:
            self.users[username] = SimpleNamespace(id=f"id-{username}", username=username)
        return self.users[username]


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_resolve_user_from_request_happy_path(monkeypatch) -> None:
    settings = Settings(trusted_proxy_cidrs=["127.0.0.1/32"])
    request = _make_request({"X-Forwarded-User": "alice"})
    session = _FakeSession()
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)

    user = await resolve_user_from_request(request, session, settings)
    assert user.username == "alice"
    assert session.committed is True


@pytest.mark.asyncio
async def test_resolve_user_from_request_rejects_untrusted_proxy() -> None:
    from fastapi import HTTPException

    settings = Settings(trusted_proxy_cidrs=["10.0.0.0/8"])
    request = _make_request({"X-Forwarded-User": "alice"}, client_host="8.8.8.8")
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await resolve_user_from_request(request, session, settings)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_user_from_request_missing_header() -> None:
    from fastapi import HTTPException

    settings = Settings(trusted_proxy_cidrs=["127.0.0.1/32"], environment="prod")
    request = _make_request({})
    session = _FakeSession()

    with pytest.raises(HTTPException) as excinfo:
        await resolve_user_from_request(request, session, settings)
    assert excinfo.value.status_code == 401

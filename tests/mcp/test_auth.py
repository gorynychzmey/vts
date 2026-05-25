from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from vts.core.config import Settings
from vts.services.auth import resolve_user_from_request


def _make_request(*, headers=None, cookies=None, scheme="https", query_string=b"") -> Request:
    headers_list = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers_list.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http",
        "headers": headers_list,
        "query_string": query_string,
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


class _FakeRedis:
    """Minimal async Redis stub used by session_store-aware tests."""
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, key, value, *, ex=None) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


async def test_browser_path_resolves_via_redis_sid(monkeypatch) -> None:
    """vts-pa9: sid in cookie + record in Redis -> user materialised."""
    import json

    settings = Settings(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
    )
    redis = _FakeRedis()
    redis.store["vts:session:abc123"] = json.dumps(
        {"email": "alice@example.com", "issued_at": 12345}
    ).encode("utf-8")
    request = _make_request(cookies={"__starlette_session__": {"sid": "abc123"}})
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings, redis=redis)
    assert user.username == "alice@example.com"


async def test_browser_path_rejects_sid_missing_from_redis(monkeypatch) -> None:
    """vts-pa9: sid in cookie but record gone (logout / expiry) -> 401."""
    settings = Settings(oauth_enabled=True, oauth_allowed_domains=["example.com"])
    redis = _FakeRedis()  # empty
    request = _make_request(cookies={"__starlette_session__": {"sid": "abc123"}})
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings, redis=redis)
    assert exc.value.status_code == 401


async def test_browser_path_rejects_redis_sid_email_not_allowed(monkeypatch) -> None:
    """vts-pa9 + vts-jo2: even with a valid Redis record, if the email is
    no longer in oauth_allowed_*, deny."""
    import json

    settings = Settings(oauth_enabled=True, oauth_allowed_domains=["example.com"])
    redis = _FakeRedis()
    redis.store["vts:session:abc123"] = json.dumps(
        {"email": "alice@elsewhere.com", "issued_at": 12345}
    ).encode("utf-8")
    request = _make_request(cookies={"__starlette_session__": {"sid": "abc123"}})
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings, redis=redis)
    assert exc.value.status_code == 403


async def test_browser_path_falls_back_to_legacy_email_cookie(monkeypatch) -> None:
    """vts-pa9 backwards-compat: cookies issued before this change carry
    `email` directly and must still work for their remaining max-age."""
    settings = Settings(oauth_enabled=True, oauth_allowed_domains=["example.com"])
    request = _make_request(cookies={"__starlette_session__": {"email": "alice@example.com"}})
    repo = _FakeRepo()
    monkeypatch.setattr("vts.services.auth.Repo", lambda _s: repo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings, redis=_FakeRedis())
    assert user.username == "alice@example.com"


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


async def test_as_user_normalised_to_lower_case(monkeypatch) -> None:
    """vts-9kk / audit Finding 6: ?as_user=Alice@Example.com must look up
    the same row /auth/callback created with .strip().lower()."""
    settings = Settings(
        oauth_enabled=True,
        admin_emails=["admin@example.com"],
        oauth_allowed_domains=["example.com"],
    )
    request = _make_request(
        cookies={"__starlette_session__": {"email": "admin@example.com"}},
        query_string=b"as_user=Alice%40Example.com",
    )

    looked_up: list[str] = []

    class _RecordingRepo:
        def __init__(self, _s) -> None:
            self.users: dict = {}

        async def get_or_create_user(self, username: str):
            self.users[username] = SimpleNamespace(id=uuid.uuid4(), username=username)
            return self.users[username]

        async def get_user_by_username(self, username: str):
            looked_up.append(username)
            return SimpleNamespace(id=uuid.uuid4(), username=username)

    monkeypatch.setattr("vts.services.auth.Repo", _RecordingRepo)
    session = _FakeSession()

    user = await resolve_user_from_request(request, session, settings)
    assert looked_up == ["alice@example.com"]
    assert user.acting_as == "alice@example.com"


async def test_oauth_enabled_no_credentials_raises_401(monkeypatch) -> None:
    settings = Settings(oauth_enabled=True)
    request = _make_request()
    session = _FakeSession()
    with pytest.raises(HTTPException) as exc:
        await resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 401

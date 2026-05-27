from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import vts.services.auth as auth_mod
from vts.services.api_tokens import generate_token, hash_token


def _make_request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


class _FakeSession:
    """Async-ish stub satisfying just what _resolve_via_api_token uses:
       - session.get(User, user_id) → returns the configured User row or None
       - session.commit() → no-op
       Repo() is constructed with this session but we monkey-patch Repo entirely.
    """
    def __init__(self, user_by_id: dict[uuid.UUID, object]) -> None:
        self._users = user_by_id
        self.committed = False

    async def get(self, _model, key):
        return self._users.get(key)

    async def commit(self):
        self.committed = True


class _FakeRepo:
    """Replaces vts.services.auth.Repo. Backed by per-test state."""
    instances: list["_FakeRepo"] = []

    def __init__(self, session):
        self.session = session
        self.touched: list[uuid.UUID] = []
        _FakeRepo.instances.append(self)

    # Controlled by the test via class attrs.
    token_row: object | None = None
    user_by_username: dict[str, object] = {}

    async def get_active_api_token_by_hash(self, _h):
        return self.token_row

    async def touch_api_token_last_used(self, token_id):
        self.touched.append(token_id)

    async def get_user_by_username(self, name):
        return self.user_by_username.get(name)

    async def get_or_create_user(self, name):
        existing = self.user_by_username.get(name)
        if existing is not None:
            return existing
        new_user = SimpleNamespace(id=uuid.uuid4(), username=name)
        self.user_by_username[name] = new_user
        return new_user


@pytest.fixture(autouse=True)
def _reset_repo_state(monkeypatch):
    # Patch the Repo symbol used inside services.auth so both _resolve_via_api_token
    # and _materialize_user see the same fake.
    monkeypatch.setattr(auth_mod, "Repo", _FakeRepo)
    _FakeRepo.instances.clear()
    _FakeRepo.token_row = None
    _FakeRepo.user_by_username = {}
    # Reset the in-process touch throttle so each test starts clean.
    auth_mod._token_last_touched.clear()
    yield


def _settings(oauth_enabled=False, allowed_domains=None, allowed_emails=None, admin_emails=None):
    s = SimpleNamespace(
        oauth_enabled=oauth_enabled,
        oauth_allowed_domains=allowed_domains or [],
        oauth_allowed_emails=allowed_emails or [],
        admin_emails=admin_emails or [],
    )
    s.is_admin = lambda email: email in (admin_emails or [])
    return s


async def test_valid_api_token_authenticates_user() -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, username="alice@example.com")
    _FakeRepo.user_by_username = {"alice@example.com": user}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)

    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"})
    session = _FakeSession({user_id: user})

    result = await auth_mod.resolve_user_from_request(request, session, _settings())
    assert result.username == "alice@example.com"
    assert result.is_admin is False


async def test_unknown_token_returns_401() -> None:
    _FakeRepo.token_row = None
    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"})
    session = _FakeSession({})

    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 401


async def test_revoked_token_returns_401() -> None:
    # Revoked tokens are filtered out by Repo.get_active_api_token_by_hash;
    # the fake repo returns None — same path as unknown token.
    _FakeRepo.token_row = None
    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"})
    session = _FakeSession({})

    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower() or "invalid" in exc.value.detail.lower()


async def test_oauth_path_unaffected_by_api_token_prefix_check() -> None:
    """A non-vts_ bearer must fall through to the FastMCP path. In oauth_enabled=False
    mode, that path is never reached (X-Forwarded-User is required instead)."""
    settings = _settings(oauth_enabled=False)
    request = _make_request({"authorization": "Bearer ya29.oauth-style-token"})
    session = _FakeSession({})

    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, settings)
    # Should land on the dev-mode "Missing X-Forwarded-User" branch
    assert exc.value.status_code == 401


async def test_token_owner_outside_allowlist_returns_403_when_oauth_enabled() -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, username="kicked@evil.com")
    _FakeRepo.user_by_username = {"kicked@evil.com": user}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)

    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"})
    session = _FakeSession({user_id: user})

    settings = _settings(oauth_enabled=True, allowed_domains=["example.com"])
    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, settings)
    assert exc.value.status_code == 403


async def test_token_works_when_oauth_disabled_regardless_of_allowlist() -> None:
    """In oauth_enabled=False mode (no Google), tokens work without allow-list
    re-check. This is what makes scripted clients useful on internal deployments."""
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, username="ops@internal")
    _FakeRepo.user_by_username = {"ops@internal": user}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)

    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"})
    session = _FakeSession({user_id: user})

    settings = _settings(oauth_enabled=False)  # no allow-list at all
    result = await auth_mod.resolve_user_from_request(request, session, settings)
    assert result.username == "ops@internal"


async def test_touch_throttled_within_interval() -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, username="alice@example.com")
    _FakeRepo.user_by_username = {"alice@example.com": user}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)

    raw = generate_token()
    headers = {"authorization": f"Bearer {raw}"}
    settings = _settings()

    def total_touches() -> int:
        return sum(len(r.touched) for r in _FakeRepo.instances)

    session = _FakeSession({user_id: user})
    await auth_mod.resolve_user_from_request(_make_request(headers), session, settings)
    touches_after_first = total_touches()
    assert touches_after_first == 1  # cold cache → write

    # Second call in same process: throttle prevents another write.
    session2 = _FakeSession({user_id: user})
    await auth_mod.resolve_user_from_request(_make_request(headers), session2, settings)
    assert total_touches() == 1  # unchanged


async def test_admin_token_can_impersonate_via_as_user() -> None:
    admin_id, target_id = uuid.uuid4(), uuid.uuid4()
    admin = SimpleNamespace(id=admin_id, username="admin@example.com")
    target = SimpleNamespace(id=target_id, username="other@example.com")
    _FakeRepo.user_by_username = {admin.username: admin, target.username: target}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=admin_id)

    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"}, query="as_user=other@example.com")
    session = _FakeSession({admin_id: admin, target_id: target})

    settings = _settings(admin_emails=["admin@example.com"])
    result = await auth_mod.resolve_user_from_request(request, session, settings)
    assert result.is_admin is True
    assert result.username == "other@example.com"  # acting as target
    assert result.requested_by == "admin@example.com"


async def test_non_admin_token_cannot_impersonate() -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id, username="alice@example.com")
    _FakeRepo.user_by_username = {user.username: user}
    _FakeRepo.token_row = SimpleNamespace(id=uuid.uuid4(), user_id=user_id)

    raw = generate_token()
    request = _make_request({"authorization": f"Bearer {raw}"}, query="as_user=victim@example.com")
    session = _FakeSession({user_id: user})

    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 403

"""vts-rxy: MCP routes must reject cookie auth and require Bearer.

The web browser session cookie is sent automatically on any request to
the same host, including the /mcp sub-app. Before vts-rxy, the resolver
would accept the cookie there and bypass the Bearer allow-list re-check.
After the fix, /mcp paths refuse cookie auth with 401."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import vts.services.auth as auth_mod


def _make_request(path: str, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


class _SessionDict(dict):
    """Lets Request.session look like a normal dict (Starlette sets attr)."""
    pass


def _make_request_with_session(path: str, session_data: dict) -> Request:
    """Forge a request that already has a SessionMiddleware-style session
    attached. Bypasses SessionMiddleware so we can drive the resolver
    directly."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [],
        "session": _SessionDict(session_data),
    }
    req = Request(scope)
    return req


class _FakeRepo:
    def __init__(self, session):
        self.session = session

    async def get_user_by_username(self, _name):
        return None

    async def get_or_create_user(self, name):
        return SimpleNamespace(id=uuid.uuid4(), username=name)


@pytest.fixture(autouse=True)
def _patch_repo(monkeypatch):
    monkeypatch.setattr(auth_mod, "Repo", _FakeRepo)
    yield


def _settings(mcp_path: str = "/mcp"):
    s = SimpleNamespace(
        oauth_enabled=True,
        oauth_allowed_domains=["example.com"],
        oauth_allowed_emails=[],
        admin_emails=[],
        mcp_path=mcp_path,
    )
    s.is_admin = lambda email: False
    return s


# ---------------------------------------------------------------- helper

def test_is_mcp_path_matches_root_and_subpath():
    s = _settings(mcp_path="/mcp")
    assert auth_mod._is_mcp_path(_make_request("/mcp"), s) is True
    assert auth_mod._is_mcp_path(_make_request("/mcp/"), s) is True
    assert auth_mod._is_mcp_path(_make_request("/mcp/messages"), s) is True


def test_is_mcp_path_does_not_match_unrelated_paths():
    s = _settings(mcp_path="/mcp")
    assert auth_mod._is_mcp_path(_make_request("/api/me"), s) is False
    assert auth_mod._is_mcp_path(_make_request("/"), s) is False
    assert auth_mod._is_mcp_path(_make_request("/mcpsuffix"), s) is False
    # Host-root OAuth discovery routes (RFC 8414) are NOT classified as
    # MCP for auth purposes — they're unauthenticated by design.
    assert auth_mod._is_mcp_path(_make_request("/.well-known/oauth-authorization-server"), s) is False
    assert auth_mod._is_mcp_path(_make_request("/authorize"), s) is False


def test_is_mcp_path_handles_custom_mcp_path():
    s = _settings(mcp_path="/mcp-v2")
    assert auth_mod._is_mcp_path(_make_request("/mcp-v2/foo"), s) is True
    assert auth_mod._is_mcp_path(_make_request("/mcp/foo"), s) is False


def test_is_mcp_path_handles_trailing_slash_in_config():
    s = _settings(mcp_path="/mcp/")
    assert auth_mod._is_mcp_path(_make_request("/mcp"), s) is True
    assert auth_mod._is_mcp_path(_make_request("/mcp/foo"), s) is True


# ---------------------------------------------------------------- resolver behaviour

async def test_cookie_auth_rejected_on_mcp_path() -> None:
    request = _make_request_with_session(
        "/mcp/messages",
        {"sid": "deadbeef" * 4},
    )
    session = SimpleNamespace()
    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 401
    assert "cookie auth not accepted" in exc.value.detail.lower() or "bearer" in exc.value.detail.lower()


async def test_cookie_auth_still_works_outside_mcp() -> None:
    """Sanity check: /api/me still accepts a cookie session; only /mcp is gated."""
    request = _make_request_with_session(
        "/api/me",
        {"email": "alice@example.com"},  # legacy email-in-cookie path
    )

    class _FakeSession:
        async def commit(self):
            pass

    session = _FakeSession()
    result = await auth_mod.resolve_user_from_request(request, session, _settings())
    assert result.username == "alice@example.com"


async def test_legacy_email_cookie_also_rejected_on_mcp() -> None:
    """The legacy `email` cookie (pre-vts-pa9) must also fail on /mcp,
    not just the new sid form."""
    request = _make_request_with_session(
        "/mcp/messages",
        {"email": "alice@example.com"},
    )
    session = SimpleNamespace()
    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 401


async def test_mcp_path_with_no_auth_at_all_rejects_with_401() -> None:
    """Bare /mcp request with neither cookie nor bearer must 401."""
    request = _make_request("/mcp")
    session = SimpleNamespace()
    with pytest.raises(HTTPException) as exc:
        await auth_mod.resolve_user_from_request(request, session, _settings())
    assert exc.value.status_code == 401

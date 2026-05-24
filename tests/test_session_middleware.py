from __future__ import annotations

import pytest


def _build_app_with_oauth(monkeypatch):
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "secret-secret-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET", "this-is-the-cookie-signing-key")
    get_settings.cache_clear()
    from vts.api.main import create_app
    return create_app()


def test_session_middleware_mounted_when_oauth_enabled(monkeypatch) -> None:
    app = _build_app_with_oauth(monkeypatch)
    middleware_types = [m.cls.__name__ for m in app.user_middleware]
    assert "SessionMiddleware" in middleware_types


def test_session_middleware_uses_vts_session_cookie_name(monkeypatch) -> None:
    app = _build_app_with_oauth(monkeypatch)
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    assert sm.kwargs.get("session_cookie") == "vts_session"
    assert sm.kwargs.get("https_only") is True
    assert sm.kwargs.get("same_site") == "lax"
    assert sm.kwargs.get("max_age") == 2_592_000


def test_session_middleware_secret_derives_from_client_secret_if_unset(monkeypatch) -> None:
    """If VTS_SESSION_SECRET is unset, derive deterministically from client_secret."""
    import hashlib
    from vts.core.config import get_settings

    monkeypatch.delenv("VTS_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    expected = hashlib.blake2b(
        b"the-client-secret", key=b"vts-session-cookie", digest_size=32
    ).hexdigest()
    assert sm.kwargs["secret_key"] == expected


def test_session_middleware_not_mounted_when_oauth_disabled(monkeypatch) -> None:
    from vts.core.config import get_settings

    monkeypatch.delenv("VTS_OAUTH_ENABLED", raising=False)
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    middleware_types = [m.cls.__name__ for m in app.user_middleware]
    assert "SessionMiddleware" not in middleware_types

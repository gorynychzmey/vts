from __future__ import annotations

import os

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


def test_session_middleware_max_age_overridable_via_env(monkeypatch) -> None:
    """VTS_SESSION_MAX_AGE_DAYS overrides the 30d default; expressed in days."""
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "secret-secret-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET", "the-cookie-key-xx")
    monkeypatch.setenv("VTS_SESSION_MAX_AGE_DAYS", "7")
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    assert sm.kwargs["max_age"] == 7 * 86_400


def test_session_middleware_uses_env_secret_when_set(monkeypatch, tmp_path) -> None:
    """VTS_SESSION_SECRET env wins over the file path."""
    from vts.core.config import get_settings

    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET", "explicit-env-secret")
    monkeypatch.setenv("VTS_SESSION_SECRET_FILE", str(tmp_path / "ignored"))
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    assert sm.kwargs["secret_key"] == "explicit-env-secret"
    # File must NOT have been created when env wins.
    assert not (tmp_path / "ignored").exists()


def test_session_middleware_autogenerates_secret_file_when_missing(
    monkeypatch, tmp_path
) -> None:
    """No env, no file -> generate 64-hex-char secret at session_secret_file, 0600."""
    from vts.core.config import get_settings

    secret_path = tmp_path / "state" / "session_secret"
    monkeypatch.delenv("VTS_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET_FILE", str(secret_path))
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")

    assert secret_path.exists()
    content = secret_path.read_text(encoding="utf-8").strip()
    # token_hex(32) -> 64 hex chars.
    assert len(content) == 64
    assert all(c in "0123456789abcdef" for c in content)
    assert sm.kwargs["secret_key"] == content
    mode = secret_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_session_middleware_reads_existing_secret_file(monkeypatch, tmp_path) -> None:
    """If file exists, read it; do not regenerate."""
    from vts.core.config import get_settings

    secret_path = tmp_path / "state" / "session_secret"
    secret_path.parent.mkdir(parents=True)
    existing = "deadbeef" * 8  # 64 hex chars
    secret_path.write_text(existing, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    mtime_before = secret_path.stat().st_mtime_ns

    monkeypatch.delenv("VTS_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET_FILE", str(secret_path))
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")

    assert sm.kwargs["secret_key"] == existing
    assert secret_path.stat().st_mtime_ns == mtime_before


def test_session_secret_does_NOT_derive_from_client_secret(monkeypatch, tmp_path) -> None:
    """Regression: the deterministic blake2b(client_secret) fallback must be gone."""
    import hashlib
    from vts.core.config import get_settings

    secret_path = tmp_path / "state" / "session_secret"
    monkeypatch.delenv("VTS_SESSION_SECRET", raising=False)
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "x.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_SESSION_SECRET_FILE", str(secret_path))
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    sm = next(m for m in app.user_middleware if m.cls.__name__ == "SessionMiddleware")

    legacy_blake2b = hashlib.blake2b(
        b"the-client-secret", key=b"vts-session-cookie", digest_size=32
    ).hexdigest()
    assert sm.kwargs["secret_key"] != legacy_blake2b


def test_session_middleware_not_mounted_when_oauth_disabled(monkeypatch) -> None:
    from vts.core.config import get_settings

    monkeypatch.delenv("VTS_OAUTH_ENABLED", raising=False)
    get_settings.cache_clear()
    from vts.api.main import create_app
    app = create_app()
    middleware_types = [m.cls.__name__ for m in app.user_middleware]
    assert "SessionMiddleware" not in middleware_types

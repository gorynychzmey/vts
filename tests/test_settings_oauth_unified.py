from __future__ import annotations

import pytest

from vts.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_canonical_keys(monkeypatch) -> None:
    monkeypatch.setenv("VTS_PUBLIC_BASE_URL", "https://vts.example")
    monkeypatch.setenv("VTS_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "id.apps")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_SECRET", "shh")
    monkeypatch.setenv("VTS_OAUTH_ALLOWED_DOMAINS", "vostrikov.de,vostrikov.dev")
    monkeypatch.setenv("VTS_SESSION_SECRET", "abc")
    s = Settings()
    assert s.public_base_url == "https://vts.example"
    assert s.oauth_enabled is True
    assert s.oauth_client_id == "id.apps"
    assert s.oauth_client_secret == "shh"
    assert s.oauth_allowed_domains == ["vostrikov.de", "vostrikov.dev"]
    assert s.session_secret == "abc"


def test_mcp_alias_for_client_id(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_ID", "legacy-id")
    s = Settings()
    assert s.oauth_client_id == "legacy-id"


def test_mcp_alias_for_client_secret(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_SECRET", "legacy-secret")
    s = Settings()
    assert s.oauth_client_secret == "legacy-secret"


def test_mcp_alias_for_enabled(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_ENABLED", "true")
    s = Settings()
    assert s.oauth_enabled is True


def test_mcp_alias_for_lists(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_ALLOWED_DOMAINS", "a.com,b.com")
    monkeypatch.setenv("VTS_MCP_OAUTH_ALLOWED_EMAILS", "x@y.com")
    s = Settings()
    assert s.oauth_allowed_domains == ["a.com", "b.com"]
    assert s.oauth_allowed_emails == ["x@y.com"]


def test_canonical_wins_over_alias(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_ID", "legacy")
    monkeypatch.setenv("VTS_OAUTH_CLIENT_ID", "canonical")
    s = Settings()
    assert s.oauth_client_id == "canonical"


def test_base_url_alias_splits(monkeypatch) -> None:
    """Legacy VTS_MCP_OAUTH_BASE_URL=...domain/mcp → public_base_url=domain (strip /mcp suffix)."""
    monkeypatch.setenv("VTS_MCP_OAUTH_BASE_URL", "https://vts.example/mcp")
    monkeypatch.setenv("VTS_MCP_PATH", "/mcp")
    s = Settings()
    assert s.public_base_url == "https://vts.example"


def test_trusted_proxy_cidrs_field_removed() -> None:
    s = Settings()
    assert not hasattr(s, "trusted_proxy_cidrs")
    assert not hasattr(s, "is_trusted_proxy")

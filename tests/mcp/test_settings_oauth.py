from __future__ import annotations

import pytest

from vts.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_oauth_defaults_disabled() -> None:
    s = Settings()
    assert s.oauth_enabled is False
    assert s.oauth_client_id is None
    assert s.oauth_client_secret is None
    assert s.public_base_url is None
    assert s.oauth_allowed_emails == []
    assert s.oauth_allowed_domains == []


def test_oauth_enabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.setenv("VTS_MCP_OAUTH_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("VTS_MCP_OAUTH_BASE_URL", "https://vts.example/mcp")
    s = Settings()
    assert s.oauth_enabled is True
    assert s.oauth_client_id == "abc.apps.googleusercontent.com"
    assert s.oauth_client_secret == "secret-value"
    assert s.public_base_url == "https://vts.example"


def test_allowed_lists_accept_csv(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_ALLOWED_EMAILS", " a@b.com , c@d.com ")
    monkeypatch.setenv("VTS_MCP_OAUTH_ALLOWED_DOMAINS", "x.com,y.com")
    s = Settings()
    assert s.oauth_allowed_emails == ["a@b.com", "c@d.com"]
    assert s.oauth_allowed_domains == ["x.com", "y.com"]


def test_allowed_lists_accept_json_array(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_OAUTH_ALLOWED_EMAILS", '["a@b.com", "c@d.com"]')
    s = Settings()
    assert s.oauth_allowed_emails == ["a@b.com", "c@d.com"]


def test_allowed_lists_accept_python_list_via_yaml_overrides() -> None:
    # Simulating how _load_yaml_overrides passes a real list into Settings(**overrides)
    s = Settings(oauth_allowed_domains=["vostrikov.de", "vostrikov.dev"])
    assert s.oauth_allowed_domains == ["vostrikov.de", "vostrikov.dev"]

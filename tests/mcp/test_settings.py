from __future__ import annotations

from vts.core.config import Settings


def test_mcp_defaults_enabled_at_root() -> None:
    s = Settings()
    assert s.mcp_enabled is True
    assert s.mcp_path == "/mcp"


def test_mcp_can_be_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("VTS_MCP_ENABLED", "false")
    monkeypatch.setenv("VTS_MCP_PATH", "/foo")
    s = Settings()
    assert s.mcp_enabled is False
    assert s.mcp_path == "/foo"

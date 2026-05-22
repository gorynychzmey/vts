from __future__ import annotations


def test_fastmcp_importable() -> None:
    """Smoke test: fastmcp is installed and exposes FastMCP."""
    from fastmcp import FastMCP

    mcp = FastMCP(name="vts-test")
    assert mcp.name == "vts-test"

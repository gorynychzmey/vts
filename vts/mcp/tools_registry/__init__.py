"""MCP tool registrations, split by domain.

Each module exposes `register(mcp)` and declares its tools inside it. They were
one 478-line `build_mcp_server()`; the only thing the tool bodies ever took
from that enclosing scope was `mcp` itself, so passing it in is the whole
mechanism — no other state moved.

The tool bodies are unchanged: each opens its own DB session and calls
`mcp_authenticate`, so they share nothing but the decorator.
"""

from vts.mcp.tools_registry import delivery, presets, prompts, search, tasks

__all__ = ["delivery", "presets", "prompts", "search", "tasks"]

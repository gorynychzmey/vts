"""MCP Apps views (io.modelcontextprotocol/ui): the HTML resources a host
renders in a sandboxed iframe next to a tool's result.

Each view is assembled like the /player page: real .html/.css/.js files,
inlined into one self-contained document when the resource is read, with no
bundler (spec: docs/superpowers/specs/2026-11-12-mcp-apps-design.md, 4.2.1).
The player view shares its rendering and seeking with /player through
vts/api/_templates/player_core.js.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLAYER_CORE = _HERE.parent.parent / "api" / "_templates" / "player_core.js"
_PLACEHOLDER = re.compile(r"__(CSS|JS)__")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def player_view_html() -> str:
    """The `ui://vts/player` document. Static: data arrives over the bridge,
    never baked in, so a host can cache and pre-check it."""
    js = "\n".join(
        _read(p)
        for p in (_PLAYER_CORE, _HERE / "bridge.js", _HERE / "player_view.js")
    )
    values = {"CSS": _read(_HERE / "player.css"), "JS": js}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], _read(_HERE / "player.html"))

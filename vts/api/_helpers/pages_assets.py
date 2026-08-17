"""Static assets and the rendered privacy page.

Used by `vts.api.routers.pages` (and `NO_CACHE_HEADERS` by
`vts.api.routers.meta`).
"""

from __future__ import annotations

import html as _html
import logging
from pathlib import Path

from vts.core.config import Settings

logger = logging.getLogger(__name__)

#: Bundled frontend assets (vts/static). Resolved from THIS file's location:
#: this module sits at vts/api/_helpers/, so the package root is parents[2].
#: Copying this line to a different depth silently points it elsewhere.
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

def _operator_block_html(settings: Settings) -> str:
    """Build the operator-specific block that prepends the rendered
    privacy page. Falls back to a neutral note if no operator details
    are configured."""
    name = (settings.operator_name or "").strip()
    contact = (settings.operator_contact or "").strip()
    instance = (settings.operator_instance_name or "").strip()
    if not any((name, contact, instance)):
        return (
            "<aside class='operator-block'>"
            "<p><em>This deployment did not publish operator details. "
            "Ask whoever gave you the link for their contact channel "
            "and access policy.</em></p>"
            "</aside>"
        )
    parts = ["<aside class='operator-block'>", "<h2>On this deployment</h2>", "<ul>"]
    if instance:
        parts.append(f"<li><strong>Instance:</strong> {_html.escape(instance)}</li>")
    if name:
        parts.append(f"<li><strong>Operator:</strong> {_html.escape(name)}</li>")
    if contact:
        parts.append(f"<li><strong>Contact:</strong> {_html.escape(contact)}</li>")
    parts.extend(["</ul>", "</aside>"])
    return "".join(parts)

#: Repo-root PRIVACY.md. parents[3] from vts/api/_helpers/ — the old
#: parents[1]/".." was relative to vts/api/, so it must be re-derived here,
#: not copied.
_PRIVACY_MD_PATH = Path(__file__).resolve().parents[3] / "PRIVACY.md"

#: Rendered once, then cached for the process.
_PRIVACY_TEMPLATE_HTML: str | None = None

def _privacy_template_html() -> str:
    """Read PRIVACY.md off disk once and convert to HTML."""
    global _PRIVACY_TEMPLATE_HTML
    if _PRIVACY_TEMPLATE_HTML is not None:
        return _PRIVACY_TEMPLATE_HTML
    from markdown_it import MarkdownIt
    try:
        md_text = _PRIVACY_MD_PATH.resolve().read_text(encoding="utf-8")
    except FileNotFoundError:
        md_text = "# Privacy policy\n\n_PRIVACY.md not found in deployment._\n"
    _PRIVACY_TEMPLATE_HTML = MarkdownIt("commonmark").render(md_text)
    return _PRIVACY_TEMPLATE_HTML

def _render_privacy_page(settings: Settings) -> str:
    """Render the public /privacy HTML — operator block + rendered template."""
    operator_html = _operator_block_html(settings)
    body = _privacy_template_html()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Privacy policy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 740px;
    margin: 2rem auto; padding: 0 1rem; line-height: 1.55;
    color: #222; background: #fafaf7; }}
  h1, h2, h3 {{ margin-top: 1.6em; }}
  h1 {{ font-size: 1.8rem; }}
  h2 {{ font-size: 1.3rem; }}
  table {{ border-collapse: collapse; margin: 1em 0; }}
  th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.7rem; text-align: left; }}
  code {{ background: #eee; padding: 0 0.25em; border-radius: 3px; }}
  aside.operator-block {{
    border: 1px solid #c89; background: #fdf6f0;
    border-radius: 6px; padding: 0.75rem 1rem; margin: 1.5rem 0;
  }}
  aside.operator-block h2 {{ margin: 0 0 0.5em; font-size: 1.1rem; }}
  aside.operator-block ul {{ margin: 0; padding-left: 1.2em; }}
</style>
</head>
<body>
{operator_html}
{body}
</body>
</html>"""

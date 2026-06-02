from __future__ import annotations

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[1] / "vts" / "static" / "styles.css"


def _rule_blocks(css: str, selector_substring: str) -> list[tuple[str, str]]:
    """Return (selector, body) for every rule whose selector contains the
    given substring. Naive brace matcher — adequate for our flat stylesheet."""
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selector = match.group(1).strip()
        body = match.group(2)
        if selector_substring in selector:
            blocks.append((selector, body))
    return blocks


def test_task_name_edit_display_rule_respects_hidden() -> None:
    """Regression: `.task-name-edit { display: inline-flex }` was declared
    after the global `.hidden { display: none }` with equal specificity, so
    the cascade kept the editor visible even when collapsed. Any rule that
    sets `display` on `.task-name-edit` must scope itself with `:not(.hidden)`
    (mirroring the `.btn-menu` pattern) so `.hidden` still wins."""
    css = STYLES.read_text(encoding="utf-8")
    offenders: list[str] = []
    for selector, body in _rule_blocks(css, ".task-name-edit"):
        if re.search(r"(^|[\s;])display\s*:", body) and ":not(.hidden)" not in selector:
            offenders.append(selector)
    assert not offenders, (
        "These .task-name-edit rules set `display` without `:not(.hidden)`, so "
        f"they override `.hidden` and leak the editor when collapsed: {offenders}"
    )


def test_hidden_utility_still_means_display_none() -> None:
    """Guards the assumption the rule above relies on: the global `.hidden`
    utility uses `display: none`."""
    css = STYLES.read_text(encoding="utf-8")
    hidden_rules = _rule_blocks(css, ".hidden")
    bare = [body for sel, body in hidden_rules if sel == ".hidden"]
    assert bare, ".hidden utility rule not found in styles.css"
    assert any("display" in body and "none" in body for body in bare), (
        ".hidden no longer maps to display:none — the task-name-edit "
        ":not(.hidden) guard assumes it does"
    )

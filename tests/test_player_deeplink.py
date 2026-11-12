"""Deep links into the player (vts-5yyo / VOS-134).

The mechanism this needs mostly exists (VOS-111): clicking a sentence seeks the
media, the playing sentence is highlighted, and the list autoscrolls. What was
missing is ADDRESSING — there was no way to arrive at the page already
positioned, and the highlight was driven by playback time rather than by which
fragment was asked for.

That is the whole scope: a URL that names a moment, and a highlight that can be
pointed at a fragment independently of where playback happens to be. The player
is extended, not rebuilt.

A citation is only a citation if following it lands on the passage. These tests
cover the addressing half; the browser scenario covers the behaviour.
"""
from __future__ import annotations

import re

from vts.api._templates import __name__ as _templates_pkg  # noqa: F401


def _player_js() -> str:
    """The player's script as the page carries it: the shared core (rendering,
    seeking, citing) followed by the /player adapter (URL, language)."""
    from pathlib import Path

    import vts.api.routers.artifacts as artifacts

    templates = Path(artifacts.__file__).resolve().parent.parent / "_templates"
    return "\n".join(
        (templates / name).read_text(encoding="utf-8")
        for name in ("player_core.js", "player.js")
    )


def test_the_player_reads_a_time_from_the_url():
    """?t= / #t= is the entry point a citation links to."""
    js = _player_js()
    assert "location" in js, "the player still ignores the URL entirely"
    assert re.search(r"URLSearchParams|location\.search", js), (
        "no query-string parsing: a link cannot carry a position"
    )
    assert "hash" in js, "no fragment parsing: #t= links do not work"


def test_the_player_can_highlight_a_named_cue():
    """Highlighting must be addressable, not only time-driven.

    "Highlight THIS fragment" is what a citation means; deriving it from
    currentTime alone cannot express it, since a paused player at 0:00 would
    highlight the first sentence regardless of what was cited.
    """
    js = _player_js()
    assert "data-cue" in js or "cueIndex" in js or "cue=" in js, (
        "no way to address a specific cue"
    )
    assert "cited" in js, "no distinct marker for the cited fragment"


def test_the_cited_highlight_is_not_the_same_class_as_the_playing_one():
    # They mean different things and can be on different sentences at once:
    # one is "what you followed a link to", the other "what is playing now".
    js = _player_js()
    assert '"cited"' in js or "'cited'" in js
    assert "active" in js


def test_a_missing_or_malformed_time_is_ignored_rather_than_seeking_to_zero():
    js = _player_js()
    # isNaN guards the parse; a bad ?t=abc must leave the player where it is.
    assert "isNaN" in js


# Cue numbering (consecutive across blocks, empty blocks and unusable sentences
# take no number, the same numbers after a live rebuild) is rendered in the
# browser by player_core.js, the page's only renderer. It is checked there, in
# tests/ui/scenarios/player-rebuild-numbering.mjs and player-behaviour.mjs.

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
    from pathlib import Path

    import vts.api.routers.artifacts as artifacts

    return (Path(artifacts.__file__).resolve().parent.parent / "_templates" / "player.js").read_text(
        encoding="utf-8"
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


def test_cues_are_numbered_consecutively_across_blocks():
    """`data-cue` names a sentence in the RECORDING, not in its block.

    Numbering per block would make ?cue=2 ambiguous — every block has one — so
    a citation would land on the wrong passage as soon as a recording had more
    than one speaker turn.
    """
    from vts.api.routers.artifacts import _player_block_html

    blocks = [
        {"label": "Анна", "sentences": [
            {"start": 0.0, "end": 2.0, "text": "первое"},
            {"start": 2.0, "end": 4.0, "text": "второе"},
        ]},
        {"label": "Борис", "sentences": [
            {"start": 4.0, "end": 6.0, "text": "третье"},
        ]},
    ]
    html_parts, next_index = [], 0
    for block in blocks:
        html, next_index = _player_block_html(block, next_index)
        html_parts.append(html)
    rendered = "".join(html_parts)

    assert 'data-cue="0"' in rendered
    assert 'data-cue="1"' in rendered
    assert 'data-cue="2"' in rendered, "the second block restarted the numbering"
    assert next_index == 3


def test_an_empty_block_does_not_consume_a_cue_number():
    # A block that renders nothing must not create a gap: the numbers have to
    # match what the live rebuild produces, and that skips empty blocks too.
    from vts.api.routers.artifacts import _player_block_html

    html, next_index = _player_block_html({"sentences": []}, 5)
    assert html == ""
    assert next_index == 5


def test_the_live_rebuild_numbers_cues_the_same_way():
    """The two render paths must agree, or a citation breaks on rebuild.

    The server renders the page; player_live.js rebuilds the list when the
    transcript changes. If only one of them numbered cues, a link followed
    before the rebuild would point at nothing after it.
    """
    from pathlib import Path

    import vts.api.routers.artifacts as artifacts

    live = (
        Path(artifacts.__file__).resolve().parent.parent / "_templates" / "player_live.js"
    ).read_text(encoding="utf-8")
    assert "data-cue" in live, "the live rebuild drops the cue addressing"
    # Counted across the transcript, and reset when the list is rebuilt.
    assert "cueCounter" in live
    assert "cueCounter = 0" in live

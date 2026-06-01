from __future__ import annotations

from vts.api.main import _MAX_DISPLAY_NAME_CHARS, normalize_display_name


def test_none_stays_none() -> None:
    # No display_name supplied → fall back to source_url downstream.
    assert normalize_display_name(None) is None


def test_empty_and_whitespace_become_none() -> None:
    # An empty form field must not produce a blank title that shadows source_url.
    assert normalize_display_name("") is None
    assert normalize_display_name("   ") is None
    assert normalize_display_name("\t\n ") is None


def test_valid_name_is_trimmed() -> None:
    assert normalize_display_name("  Стендап 2026-06-01  ") == "Стендап 2026-06-01"


def test_interior_whitespace_is_preserved() -> None:
    assert normalize_display_name("Team sync  call") == "Team sync  call"


def test_overlong_name_is_capped() -> None:
    raw = "x" * (_MAX_DISPLAY_NAME_CHARS + 50)
    result = normalize_display_name(raw)
    assert result is not None
    assert len(result) == _MAX_DISPLAY_NAME_CHARS

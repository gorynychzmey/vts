"""Tests for the /privacy page renderer.

Covers two things: that operator details from config render into the
page, and that absence of operator details produces a neutral block
(no leaked personal data from the source repo)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def main_module():
    from vts.api import main  # noqa: F401
    return main


def _settings(**overrides):
    base = {
        "operator_name": None,
        "operator_contact": None,
        "operator_instance_name": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_operator_block_neutral_when_unset(main_module):
    html = main_module._operator_block_html(_settings())
    assert "did not publish operator details" in html
    # Must not accidentally leak any of these from the repo template.
    assert "victor@vostrikov.de" not in html
    assert "Vostrikov" not in html


def test_operator_block_renders_all_three_fields(main_module):
    html = main_module._operator_block_html(_settings(
        operator_name="Alice Example",
        operator_contact="alice@example.com",
        operator_instance_name="vts.example.com",
    ))
    assert "Alice Example" in html
    assert "alice@example.com" in html
    assert "vts.example.com" in html
    assert "did not publish" not in html


def test_operator_block_partial_fields_only_render_those(main_module):
    html = main_module._operator_block_html(_settings(operator_name="Alice"))
    assert "Alice" in html
    assert "Contact:" not in html
    assert "Instance:" not in html


def test_operator_block_escapes_html(main_module):
    """A malicious-looking operator name must not break out of the block."""
    html = main_module._operator_block_html(_settings(
        operator_name="<script>alert(1)</script>",
    ))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_privacy_page_includes_template_body(main_module):
    page = main_module._render_privacy_page(_settings())
    # Template heading should make it through markdown rendering.
    assert "<h1>" in page
    assert "Privacy policy" in page
    # Neutral operator note should be present when nothing is configured.
    assert "did not publish operator details" in page


def test_render_privacy_page_does_not_contain_authors_personal_data(main_module):
    """The shipped PRIVACY.md must stay free of any single operator's
    personal data — that information belongs in env vars on each
    deployment, not in the source repo."""
    page = main_module._render_privacy_page(_settings())
    assert "victor@vostrikov.de" not in page
    assert "Viktor Vostrikov" not in page

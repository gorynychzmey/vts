from __future__ import annotations

from vts.services.preset_expand import expand_preset_options


def test_delivery_survives_expansion():
    """expand_preset_options is an allowlist — a new field silently vanishes
    unless it is added explicitly. That trap is what this test guards."""
    opts = {"delivery": [{"deliver_to": "out", "variant": "summary"}]}
    out = expand_preset_options(opts, valid_user_prompt_ids=set())
    assert out["delivery"] == [{"deliver_to": "out", "variant": "summary"}]


def test_delivery_defaults_empty():
    out = expand_preset_options({}, valid_user_prompt_ids=set())
    assert out["delivery"] == []


def test_delivery_entries_without_deliver_to_are_dropped():
    out = expand_preset_options(
        {"delivery": [{"variant": "raw"}, {"deliver_to": "keep"}, "junk", None]},
        valid_user_prompt_ids=set(),
    )
    assert out["delivery"] == [{"deliver_to": "keep"}]


def test_delivery_variant_is_optional_and_preserved():
    out = expand_preset_options(
        {"delivery": [{"deliver_to": "a"}, {"deliver_to": "b", "variant": "raw"}]},
        valid_user_prompt_ids=set(),
    )
    assert out["delivery"] == [{"deliver_to": "a"}, {"deliver_to": "b", "variant": "raw"}]


def test_delivery_carries_no_secrets():
    """Presets store target NAMES only; secrets live on the target row."""
    out = expand_preset_options(
        {"delivery": [{"deliver_to": "out", "secrets": {"api_token": "leak"}}]},
        valid_user_prompt_ids=set(),
    )
    assert out["delivery"] == [{"deliver_to": "out"}]

"""Merging a credential with a target, and validating the result (vts-929).

The adapter is handed one flat config, so it never learns that the connection
and the destination are stored separately. These tests pin the merge rules and
the fact that validation runs on the MERGED view.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from vts.services.delivery_config import (
    DeliveryConfigInvalid,
    merge_config,
    split_by_connection,
    validate_config,
)


class _Adapter:
    name = "fake"

    def config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "collection_id": {"type": "string"},
            },
            "required": ["base_url", "collection_id"],
        }

    def secret_keys(self) -> list[str]:
        return ["api_token"]

    def connection_fields(self) -> list[str]:
        return ["base_url", "api_token"]


def _row(**config):
    return SimpleNamespace(config_json=config)


def test_merge_combines_both_halves():
    merged = merge_config(
        _row(base_url="https://o.example"), _row(collection_id="c1")
    )
    assert merged == {"base_url": "https://o.example", "collection_id": "c1"}


def test_target_overrides_the_credential():
    """A per-destination value is more specific than the shared connection."""
    merged = merge_config(
        _row(base_url="https://shared.example", timeout=30),
        _row(base_url="https://override.example"),
    )
    assert merged["base_url"] == "https://override.example"
    assert merged["timeout"] == 30


def test_merge_tolerates_missing_sides():
    assert merge_config(None, _row(a=1)) == {"a": 1}
    assert merge_config(_row(a=1), None) == {"a": 1}


def test_merge_does_not_mutate_its_inputs():
    """The rows are SQLAlchemy JSON columns; mutating one in place would
    persist a change nobody asked for (see the project's JSON-column rule)."""
    credential = _row(base_url="https://o.example")
    target = _row(collection_id="c1")
    merge_config(credential, target)
    assert credential.config_json == {"base_url": "https://o.example"}
    assert target.config_json == {"collection_id": "c1"}


def test_split_uses_the_adapters_declared_boundary():
    connection, params = split_by_connection(
        _Adapter(),
        {"base_url": "u", "api_token": "t", "collection_id": "c", "default_variant": "raw"},
    )
    assert connection == {"base_url": "u", "api_token": "t"}
    assert params == {"collection_id": "c", "default_variant": "raw"}


def test_split_with_no_connection_fields_puts_everything_in_params():
    """An adapter that needs no connection (e.g. delivery to a local folder)
    declares an empty set, and nothing is treated as credential material."""
    class NoConnection(_Adapter):
        def connection_fields(self) -> list[str]:
            return []

    connection, params = split_by_connection(NoConnection(), {"path": "/tmp/out"})
    assert connection == {}
    assert params == {"path": "/tmp/out"}


def test_validation_passes_on_the_merged_config():
    validate_config(_Adapter(), {"base_url": "u", "collection_id": "c"})


def test_validation_fails_on_either_half_alone():
    """The reason validation must run on the merge, not on the parts.

    A target legitimately has no `base_url` — it lives on the credential — so
    validating a target by itself would reject every valid target. The mirror
    also holds for a credential without the destination field.
    """
    with pytest.raises(DeliveryConfigInvalid):
        validate_config(_Adapter(), {"collection_id": "c"})  # target's half
    with pytest.raises(DeliveryConfigInvalid):
        validate_config(_Adapter(), {"base_url": "u"})  # credential's half


def test_validation_message_names_the_offending_field():
    with pytest.raises(DeliveryConfigInvalid) as exc:
        validate_config(_Adapter(), {"base_url": "u"})
    assert "collection_id" in str(exc.value)


def test_wrong_type_is_rejected():
    with pytest.raises(DeliveryConfigInvalid):
        validate_config(_Adapter(), {"base_url": 42, "collection_id": "c"})


def test_empty_schema_accepts_anything():
    class NoSchema(_Adapter):
        def config_schema(self) -> dict:
            return {}

    validate_config(NoSchema(), {"whatever": True})


def test_broken_schema_blames_the_adapter_not_the_user():
    """A malformed schema is the plugin author's bug. Saying "your config is
    invalid" would send the user hunting through settings they got right."""
    class BadSchema(_Adapter):
        def config_schema(self) -> dict:
            return {"type": "not-a-real-type"}

    with pytest.raises(DeliveryConfigInvalid) as exc:
        validate_config(BadSchema(), {"anything": 1})
    assert "schema" in str(exc.value).lower()

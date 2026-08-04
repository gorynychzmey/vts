"""Merge a delivery credential with a target, and validate the result (vts-929).

A target holds only per-destination parameters; the endpoint and its secrets
live in a shared credential. The adapter is handed the flat, merged view it
has always received, so plugins are unaware of the split.

Kept in services because REST, MCP and the delivery consumer must all merge
and validate identically — the same reason validate_delivery_refs lives here.
"""
from __future__ import annotations

from typing import Any


class DeliveryConfigInvalid(ValueError):
    """The merged config does not satisfy the adapter's schema."""


def merge_config(credential: Any, target: Any) -> dict[str, Any]:
    """Flatten credential config + target config into what the adapter sees.

    The target wins on conflict: a per-destination override is more specific
    than the shared connection it hangs off.
    """
    merged: dict[str, Any] = dict((credential.config_json if credential else {}) or {})
    merged.update((target.config_json if target else {}) or {})
    return merged


def split_by_connection(
    adapter: Any, config: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a flat config into (connection fields, per-target parameters).

    The boundary comes from the adapter, never from the core — see
    DeliveryAdapter.connection_fields.
    """
    names = set(adapter.connection_fields())
    connection = {k: v for k, v in config.items() if k in names}
    params = {k: v for k, v in config.items() if k not in names}
    return connection, params


def validate_config(adapter: Any, merged: dict[str, Any]) -> None:
    """Check the MERGED config against the adapter's JSON Schema.

    Validating the merge rather than either half is the whole point: a target
    alone legitimately lacks `base_url`, which Outline's schema marks required,
    so validating a target on its own would reject every valid target.

    Raises DeliveryConfigInvalid with a readable message.
    """
    schema = adapter.config_schema()
    if not schema:
        return
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - declared dependency
        # Never fail a delivery because the validator is unavailable; the
        # adapter still performs its own key lookups.
        return

    try:
        jsonschema.validate(instance=merged, schema=schema)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.absolute_path) or "config"
        raise DeliveryConfigInvalid(f"{where}: {exc.message}") from exc
    except jsonschema.SchemaError as exc:
        # A broken schema is the plugin's bug, not the user's input. Say so
        # rather than blaming the config the user just typed.
        raise DeliveryConfigInvalid(
            f"adapter {getattr(adapter, 'name', '?')!r} has an invalid config schema: {exc.message}"
        ) from exc

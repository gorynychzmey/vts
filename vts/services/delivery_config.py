"""Merge a delivery credential with a target, and validate the result (vts-929).

A target holds only per-destination parameters; the endpoint and its secrets
live in a shared credential. The adapter is handed the flat, merged view it
has always received, so plugins are unaware of the split.

Kept in services because REST, MCP and the delivery consumer must all merge
and validate identically — the same reason validate_delivery_refs lives here.
"""
from __future__ import annotations

from typing import Any


#: Config keys the CORE owns and interprets, which no adapter should declare
#: or read (vts-6fya).
#:
#: `default_variant` picks WHICH artifact of a task gets delivered. The core
#: resolves it — including prompt-result refs, whose valid values are
#: per-user and could never be baked into a plugin's schema. An adapter
#: receives the resolved content in DeliveryPayload and has no use for the
#: name of the variant it came from.
CORE_CONFIG_FIELDS = frozenset({"default_variant"})


class DeliveryConfigInvalid(ValueError):
    """The merged config does not satisfy the adapter's schema."""


def strip_core_fields(config: dict[str, Any]) -> dict[str, Any]:
    """Drop core-owned keys, leaving what the adapter actually declares.

    These live in the same `config_json` as the adapter's own settings — one
    JSON column holds a target's whole configuration — but they are not part
    of the adapter's schema, so they must not be validated against it or
    handed to the adapter.
    """
    return {k: v for k, v in (config or {}).items() if k not in CORE_CONFIG_FIELDS}


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

    Core-owned keys are removed first: a strict schema
    (`additionalProperties: false`) would otherwise reject a target for
    carrying `default_variant`, which the core put there and the adapter was
    never told about. Outline's schema is permissive, so this would have
    surfaced only once some third-party plugin tightened its own — a failure
    that would have looked like the user's fault.

    Raises DeliveryConfigInvalid with a readable message.
    """
    schema = adapter.config_schema()
    if not schema:
        return
    merged = strip_core_fields(merged)
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

"""Validation of a submit-time `delivery` list. Shared by REST and MCP.

Kept out of the endpoints so both surfaces enforce the same invariants — the
same reason preset expansion and status predicates live in services.
"""
from __future__ import annotations

import uuid
from typing import Any


class DeliveryValidationError(ValueError):
    """A submitted delivery list cannot be honoured. Carries a user-facing message."""


def delivery_target_view(target: Any, settings: Any) -> dict:
    """Serialise a DeliveryTarget for an API/MCP response, WITHOUT secret values.

    Shared by REST and MCP so the two surfaces cannot drift on the one rule that
    matters here: secrets are write-only. Only presence booleans are emitted.

    A target whose adapter is not loaded still serialises — settings outlive a
    temporarily missing plugin. Its secret key names then come from what is
    stored, since the adapter is no longer there to declare them.
    """
    from vts.delivery.registry import UnknownAdapter, get_adapter

    adapter_available = True
    try:
        keys = list(get_adapter(target.adapter).secret_keys())
    except UnknownAdapter:
        adapter_available = False
        keys = []

    stored: dict[str, str] = {}
    if target.secrets_enc:
        try:
            from vts.core.secrets import decrypt_secrets, load_secrets_key

            stored = decrypt_secrets(target.secrets_enc, load_secrets_key(settings))
        except Exception:
            # No key configured or an undecryptable blob: never fail the read,
            # never leak. Presence stays unknown-but-safe.
            stored = {}
    if not keys and stored:
        keys = list(stored)

    return {
        "id": str(target.id),
        "name": target.name,
        "adapter": target.adapter,
        "config": target.config_json or {},
        "secrets": {k: {"set": bool(stored.get(k))} for k in keys},
        "adapter_available": adapter_available,
    }


async def validate_delivery_refs(
    repo: Any, user_id: uuid.UUID, delivery: list[dict] | None
) -> list[dict]:
    """Resolve each `deliver_to` to one of the user's targets.

    Raises DeliveryValidationError when a name is unknown, or when the target's
    adapter is not loaded right now. Both are checked HERE, on an EXPLICIT
    submit, so the caller finds out immediately instead of at completion.

    Deliberately NOT applied to delivery coming from a preset: a preset naming a
    target whose plugin is temporarily missing must not fail the task — that
    delivery is enqueued and parked in `waiting_adapter` until the plugin is
    back (see the spec's "Временно недоступный адаптер").
    """
    from vts.delivery.registry import UnknownAdapter, get_adapter

    if not delivery:
        return []

    out: list[dict] = []
    for item in delivery:
        if not isinstance(item, dict):
            raise DeliveryValidationError("each delivery entry must be an object")
        name = item.get("deliver_to")
        if not name:
            raise DeliveryValidationError("delivery entry requires 'deliver_to'")

        target = await repo.get_delivery_target_by_name(user_id, str(name))
        if target is None:
            raise DeliveryValidationError(f"Unknown delivery target: {name}")

        try:
            get_adapter(target.adapter)
        except UnknownAdapter as exc:
            raise DeliveryValidationError(
                f"Delivery target {name!r} uses adapter {target.adapter!r}, "
                "which is not available right now"
            ) from exc

        entry: dict = {"deliver_to": str(name)}
        variant = item.get("variant")
        if variant:
            entry["variant"] = str(variant)
        out.append(entry)
    return out

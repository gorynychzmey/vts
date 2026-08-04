"""Validation of a submit-time `delivery` list. Shared by REST and MCP.

Kept out of the endpoints so both surfaces enforce the same invariants — the
same reason preset expansion and status predicates live in services.
"""
from __future__ import annotations

import uuid
from typing import Any


class DeliveryValidationError(ValueError):
    """A submitted delivery list cannot be honoured. Carries a user-facing message."""


def delivery_credential_view(credential: Any, settings: Any, *, used_by: int = 0) -> dict:
    """Serialise a DeliveryCredential, WITHOUT secret values.

    Shared by REST and MCP so the two surfaces cannot drift on the one rule
    that matters here: secrets are write-only. Only presence booleans are
    emitted.

    A credential whose adapter is not loaded still serialises — settings
    outlive a temporarily missing plugin. Its secret key names then come from
    what is stored, since the adapter is no longer there to declare them.
    """
    from vts.delivery.registry import UnknownAdapter, get_adapter

    adapter_available = True
    try:
        keys = list(get_adapter(credential.adapter).secret_keys())
    except UnknownAdapter:
        adapter_available = False
        keys = []

    stored: dict[str, str] = {}
    if credential.secrets_enc:
        try:
            from vts.core.secrets import decrypt_secrets, load_secrets_key

            stored = decrypt_secrets(credential.secrets_enc, load_secrets_key(settings))
        except Exception:
            # No key configured or an undecryptable blob: never fail the read,
            # never leak. Presence stays unknown-but-safe.
            stored = {}
    if not keys and stored:
        keys = list(stored)

    return {
        "id": str(credential.id),
        "name": credential.name,
        "adapter": credential.adapter,
        "config": credential.config_json or {},
        "secrets": {k: {"set": bool(stored.get(k))} for k in keys},
        "adapter_available": adapter_available,
        "used_by": used_by,
    }


def delivery_target_view(target: Any, settings: Any) -> dict:
    """Serialise a DeliveryTarget for an API/MCP response.

    Secrets no longer live here — they belong to the credential this target
    references (vts-929), which is serialised by delivery_credential_view.
    """
    from vts.delivery.registry import UnknownAdapter, get_adapter

    adapter_available = True
    try:
        get_adapter(target.adapter)
    except UnknownAdapter:
        adapter_available = False

    return {
        "id": str(target.id),
        "name": target.name,
        "adapter": target.adapter,
        "credential_id": str(target.credential_id),
        "config": target.config_json or {},
        "adapter_available": adapter_available,
    }


async def validate_delivery_refs(
    repo: Any, user_id: uuid.UUID, delivery: list[dict] | None
) -> list[dict]:
    """Resolve each `deliver_to` (a target UUID) to one of the user's targets.

    `deliver_to` carries the target's id, never its name (vts-929): a name is
    for humans and may be changed at will, and a rename must not silently
    orphan queued tasks and presets that referenced the old one.

    Raises DeliveryValidationError when the id is unknown, or when the target's
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
        ref = item.get("deliver_to")
        if not ref:
            raise DeliveryValidationError("delivery entry requires 'deliver_to'")
        try:
            target_id = uuid.UUID(str(ref))
        except ValueError as exc:
            raise DeliveryValidationError(
                f"deliver_to must be a delivery target id (UUID), got {ref!r}"
            ) from exc

        target = await repo.get_delivery_target(user_id, target_id)
        if target is None:
            raise DeliveryValidationError(f"Unknown delivery target: {ref}")

        try:
            get_adapter(target.adapter)
        except UnknownAdapter as exc:
            raise DeliveryValidationError(
                f"Delivery target {target.name!r} uses adapter {target.adapter!r}, "
                "which is not available right now"
            ) from exc

        entry: dict = {"deliver_to": str(target_id)}
        variant = item.get("variant")
        if variant:
            entry["variant"] = str(variant)
        out.append(entry)
    return out

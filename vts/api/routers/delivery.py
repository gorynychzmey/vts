"""Delivery configuration and per-task delivery status (vts-ouq, vts-929).

Two related surfaces, kept together because they share the adapter/credential
helpers below:

* `/api/delivery-credentials` and `/api/delivery-targets` — how results are
  sent out and where.
* `/api/tasks/{task_id}/deliveries` — the attempts made for one task, and a
  retry trigger.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged. Where the old code closed over `create_app`'s
local `settings`, these helpers now call `get_settings()` directly; it is
`lru_cache`d, so it returns the very same object the closure captured.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api.deps import get_current_user, get_redis, get_session_dep
from vts.api.schemas import (
    DeliveryAdapterOut,
    DeliveryAdaptersOut,
    DeliveryCheckOut,
    DeliveryCredentialCreate,
    DeliveryCredentialOut,
    DeliveryCredentialUpdate,
    DeliveryOptionOut,
    DeliveryOptionsOut,
    DeliveryOut,
    DeliveryRetryRequest,
    DeliveryTargetCreate,
    DeliveryTargetOut,
    DeliveryTargetUpdate,
    DeliveryVariantOut,
)
from vts.core.config import get_settings
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

# No tags= here: _install_custom_openapi() in vts.api.main assigns the
# OpenAPI tag from the URL prefix, and an explicit router tag overrides it
# (it retagged /api/tasks/{task_id}/deliveries from "tasks" to "meta").
router = APIRouter()


async def _owned_task_or_404(repo: Repo, task_id: uuid.UUID, user: AuthenticatedUser):
    task = await repo.get_task_by_id(task_id)
    if task is None or str(task.user_id) != user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/api/tasks/{task_id}/deliveries", response_model=list[DeliveryOut])
async def list_task_deliveries_endpoint(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[DeliveryOut]:
    from vts.services.delivery_status import is_waiting_for_adapter

    repo = Repo(session)
    await _owned_task_or_404(repo, task_id, user)
    rows = await repo.list_deliveries_for_task(task_id)
    return [
        DeliveryOut(
            id=str(r.id),
            adapter=r.adapter,
            variant=r.variant,
            status=r.status.value,
            attempts=r.attempts,
            max_attempts=r.max_attempts,
            last_error=r.last_error,
            external_url=r.external_url,
            waiting_for_adapter=is_waiting_for_adapter(r.status),
        )
        for r in rows
    ]


@router.post("/api/tasks/{task_id}/deliveries/retry")
async def retry_task_deliveries_endpoint(
    task_id: uuid.UUID,
    payload: DeliveryRetryRequest | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
) -> dict:
    from vts.db.models import utcnow as _utcnow

    repo = Repo(session)
    await _owned_task_or_404(repo, task_id, user)
    target_id = payload.target_id if payload else None
    reset = await repo.reset_delivery_for_retry(task_id, target_id, _utcnow())
    await session.commit()
    # Wake the consumer. Redis is only a hint: the rows are already due, so a
    # dropped message merely delays them to the next timed tick.
    await redis.publish(f"{get_settings().redis_prefix}delivery:notify", "1")
    return {"reset": reset}

# ------------------------------------------------------------------
# DeliveryTarget CRUD (vts-ouq)
# ------------------------------------------------------------------


def _delivery_target_out(target) -> DeliveryTargetOut:
    from vts.services.delivery_submit import delivery_target_view

    return DeliveryTargetOut(**delivery_target_view(target, get_settings()))


def _encrypt_secrets_or_400(secrets: dict[str, str]) -> bytes:
    from vts.core.secrets import SecretsKeyMissing, encrypt_secrets, load_secrets_key

    try:
        return encrypt_secrets(secrets, load_secrets_key(get_settings()))
    except SecretsKeyMissing as exc:
        raise HTTPException(
            status_code=400,
            detail="VTS_SECRETS_KEY is not configured; cannot store delivery secrets",
        ) from exc


@router.get("/api/delivery-targets", response_model=list[DeliveryTargetOut])
async def list_delivery_targets_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[DeliveryTargetOut]:
    repo = Repo(session)
    rows = await repo.list_delivery_targets(uuid.UUID(user.id))
    return [_delivery_target_out(row) for row in rows]


def _adapter_or_400(name: str):
    from vts.delivery.registry import UnknownAdapter, get_adapter

    try:
        return get_adapter(name)
    except UnknownAdapter as exc:
        raise HTTPException(
            status_code=400, detail=f"Unknown delivery adapter: {name}"
        ) from exc


async def _credential_for_target_or_400(repo, user_id: uuid.UUID, credential_id: str, adapter: str):
    """Resolve and sanity-check the credential a target hangs off.

    Validated here rather than left to the foreign key so the caller gets a
    reason: a missing credential and one belonging to a different adapter
    are different mistakes, and neither should surface as an IntegrityError.
    """
    try:
        cred_uuid = uuid.UUID(str(credential_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="credential_id must be a UUID"
        ) from exc
    credential = await repo.get_delivery_credential(user_id, cred_uuid)
    if credential is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    if credential.adapter != adapter:
        raise HTTPException(
            status_code=422,
            detail=(
                f"credential is for adapter {credential.adapter!r}, "
                f"but the target uses {adapter!r}"
            ),
        )
    return credential


def _validate_merged_or_422(adapter, credential, config: dict) -> None:
    """Validate the MERGED config against the adapter's schema.

    The merge is what the adapter will actually receive; validating a
    target's own half would reject every valid target, since required
    connection fields such as `base_url` live on the credential.
    """
    from vts.services.delivery_config import DeliveryConfigInvalid, validate_config

    merged = dict((credential.config_json or {}) if credential else {})
    merged.update(config or {})
    try:
        validate_config(adapter, merged)
    except DeliveryConfigInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _delivery_variant_choices(session, user) -> list[DeliveryVariantOut]:
    """Which artifacts a target can deliver, for THIS user.

    Owned by the core, never by an adapter (vts-6fya): the fixed three are
    core artifacts, and the prompt entries are per-user, so no plugin
    schema could enumerate them. Labels are keys the UI localises rather
    than finished text, since the server does not know the user's locale.
    """
    from vts.delivery.resolve import VALID_VARIANTS

    choices = [
        DeliveryVariantOut(value=name, label=f"delivery.variant.{name}")
        for name in VALID_VARIANTS
    ]
    # Only USER prompts: the system summary is already covered by the
    # "summary" variant above, so listing it again would offer the same
    # artifact under two names. include_system=False is what enforces that —
    # the vendor prompt is a row in the user's own table (flagged is_system),
    # not a separate kind, so an unfiltered call returns it too (vts-lzt8).
    prompts = await Repo(session).list_prompts(
        uuid.UUID(user.id), include_system=False
    )
    for row in prompts:
        choices.append(
            DeliveryVariantOut(value=f"user:{row.id}", label=row.name)
        )
    return choices


@router.get("/api/delivery-adapters", response_model=DeliveryAdaptersOut)
async def list_delivery_adapters_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryAdaptersOut:
    """Installed delivery adapters and the shape of their settings.

    The UI builds credential and target forms from this: `config_schema`
    says which fields exist, `connection_fields` says which of them belong
    to the shared connection rather than the individual destination.
    """
    from vts.delivery.registry import incompatible_adapters, list_adapters

    out: list[DeliveryAdapterOut] = []
    for name, adapter in sorted(list_adapters().items()):
        try:
            # Optional since contract 1.2, read with getattr: an adapter
            # built against 1.1 has neither, and must keep working.
            option_fields = getattr(adapter, "option_fields", None)
            out.append(DeliveryAdapterOut(
                name=name,
                config_schema=adapter.config_schema() or {},
                secret_keys=list(adapter.secret_keys()),
                connection_fields=list(adapter.connection_fields()),
                option_fields=list(option_fields()) if callable(option_fields) else [],
                supports_check=callable(getattr(adapter, "check_connection", None)),
            ))
        except Exception as exc:  # noqa: BLE001 - third-party plugin code
            # One misbehaving plugin must not cost the operator the whole
            # list, same isolation rule the registry applies at load time.
            logging.getLogger(__name__).warning(
                "delivery adapter %r failed to describe itself: %s", name, exc
            )
    return DeliveryAdaptersOut(
        adapters=out,
        incompatible=incompatible_adapters(),
        variants=await _delivery_variant_choices(session, user),
    )


@router.get("/api/delivery-credentials", response_model=list[DeliveryCredentialOut])
async def list_delivery_credentials_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[DeliveryCredentialOut]:
    from vts.services.delivery_submit import delivery_credential_view

    repo = Repo(session)
    uid = uuid.UUID(user.id)
    rows = await repo.list_delivery_credentials(uid)
    out = []
    for row in rows:
        used_by = await repo.count_targets_for_credential(uid, row.id)
        out.append(DeliveryCredentialOut(
            **delivery_credential_view(row, get_settings(), used_by=used_by)
        ))
    return out


@router.post("/api/delivery-credentials", response_model=DeliveryCredentialOut)
async def create_delivery_credential_endpoint(
    payload: DeliveryCredentialCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryCredentialOut:
    from vts.services.delivery_submit import delivery_credential_view

    _adapter_or_400(payload.adapter)
    secrets_enc = _encrypt_secrets_or_400(payload.secrets) if payload.secrets else None
    repo = Repo(session)
    row = await repo.create_delivery_credential(
        uuid.UUID(user.id),
        name=payload.name.strip(),
        adapter=payload.adapter,
        config=payload.config,
        secrets_enc=secrets_enc,
    )
    await session.commit()
    return DeliveryCredentialOut(**delivery_credential_view(row, get_settings()))


@router.get("/api/delivery-credentials/{credential_id}", response_model=DeliveryCredentialOut)
async def get_delivery_credential_endpoint(
    credential_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryCredentialOut:
    from vts.services.delivery_submit import delivery_credential_view

    repo = Repo(session)
    uid = uuid.UUID(user.id)
    row = await repo.get_delivery_credential(uid, credential_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    used_by = await repo.count_targets_for_credential(uid, row.id)
    return DeliveryCredentialOut(
        **delivery_credential_view(row, get_settings(), used_by=used_by)
    )


@router.put("/api/delivery-credentials/{credential_id}", response_model=DeliveryCredentialOut)
async def update_delivery_credential_endpoint(
    credential_id: uuid.UUID,
    payload: DeliveryCredentialUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryCredentialOut:
    from vts.services.delivery_submit import delivery_credential_view

    secrets_enc = _encrypt_secrets_or_400(payload.secrets) if payload.secrets else None
    repo = Repo(session)
    uid = uuid.UUID(user.id)
    row = await repo.update_delivery_credential(
        uid, credential_id,
        name=payload.name, config=payload.config,
        secrets_enc=secrets_enc, clear_secrets=payload.clear_secrets,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    await session.commit()
    used_by = await repo.count_targets_for_credential(uid, row.id)
    return DeliveryCredentialOut(
        **delivery_credential_view(row, get_settings(), used_by=used_by)
    )


async def _call_adapter_within_budget(awaitable, *, adapter: str, what: str):
    """Await an interactive adapter call, enforcing the published limit.

    The limit comes from the contract rather than a literal so that plugin
    authors can read it (vts-6o37 followup): an adapter's own HTTP timeout
    has to fit inside the core's, and a number that lives only in the core
    is one every plugin has to guess or copy.

    A call that finishes but eats most of the budget is logged. That is the
    early warning — by the time it actually overruns, users are already
    seeing a generic failure instead of a diagnosis, because cancelling the
    call throws away whatever the adapter was about to report.
    """
    from vts.delivery.contract import ADAPTER_CALL_BUDGET_S, INTERACTIVE_CALL_LIMIT_S

    started = time.monotonic()
    try:
        return await asyncio.wait_for(awaitable, timeout=INTERACTIVE_CALL_LIMIT_S)
    finally:
        elapsed = time.monotonic() - started
        if elapsed > ADAPTER_CALL_BUDGET_S:
            logging.getLogger(__name__).warning(
                "adapter %r spent %.1fs on %s, over its %.0fs budget "
                "(core gives up at %.0fs)",
                adapter, elapsed, what, ADAPTER_CALL_BUDGET_S, INTERACTIVE_CALL_LIMIT_S,
            )


async def _credential_target_config(repo, uid: uuid.UUID, credential_id: uuid.UUID):
    """Build what an adapter needs to talk to a credential's endpoint.

    The credential is fetched for THIS user: these endpoints reach out to
    an external system using stored secrets, so an id from the URL must
    never be enough to probe someone else's Outline.
    """
    from vts.core.secrets import decrypt_secrets, load_secrets_key
    from vts.delivery.contract import DeliveryTargetConfig

    credential = await repo.get_delivery_credential(uid, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")

    secrets: dict[str, str] = {}
    if credential.secrets_enc:
        try:
            secrets = decrypt_secrets(credential.secrets_enc, load_secrets_key(get_settings()))
        except Exception as exc:  # noqa: BLE001 - missing key or bad blob
            raise HTTPException(
                status_code=400,
                detail="Stored secrets cannot be read; re-enter them for this connection",
            ) from exc
    cfg = DeliveryTargetConfig(config=dict(credential.config_json or {}), secrets=secrets)
    return credential, cfg


@router.post(
    "/api/delivery-credentials/{credential_id}/check",
    response_model=DeliveryCheckOut,
)
async def check_delivery_credential_endpoint(
    credential_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryCheckOut:
    """Test that a connection actually works, before anything is delivered.

    Without this the first sign of a broken connection is a failed
    delivery, which may be hours after the settings were saved.
    """
    from vts.delivery.contract import CheckOutcome

    repo = Repo(session)
    uid = uuid.UUID(user.id)
    credential, cfg = await _credential_target_config(repo, uid, credential_id)
    adapter = _adapter_or_400(credential.adapter)

    check = getattr(adapter, "check_connection", None)
    if not callable(check):
        # A 1.1 adapter, or one with nothing to test. Not an error.
        raise HTTPException(
            status_code=501,
            detail=f"Adapter {credential.adapter!r} does not support connection checks",
        )
    try:
        result = await _call_adapter_within_budget(
            check(cfg), adapter=credential.adapter, what="check_connection"
        )
    except asyncio.TimeoutError:
        return DeliveryCheckOut(ok=False, outcome=CheckOutcome.timeout.value)
    except Exception as exc:  # noqa: BLE001 - third-party plugin code
        # A plugin that raises instead of reporting is a plugin bug, but
        # the user still deserves an answer rather than a 500.
        logging.getLogger(__name__).warning(
            "adapter %r raised during check_connection: %s", credential.adapter, exc
        )
        return DeliveryCheckOut(
            ok=False, outcome=CheckOutcome.error.value, detail=str(exc)[:300]
        )

    outcome = getattr(result, "outcome", None)
    return DeliveryCheckOut(
        ok=bool(getattr(result, "ok", False)),
        outcome=getattr(outcome, "value", None) or str(outcome or CheckOutcome.error.value),
        detail=getattr(result, "detail", None),
    )


@router.get(
    "/api/delivery-credentials/{credential_id}/options/{field}",
    response_model=DeliveryOptionsOut,
)
async def delivery_field_options_endpoint(
    credential_id: uuid.UUID,
    field: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryOptionsOut:
    """Values an adapter can enumerate for one config field.

    Bound to a CREDENTIAL rather than a target because the list is needed
    while the target form is still being filled in — the target does not
    exist yet, but its credential does.
    """
    repo = Repo(session)
    uid = uuid.UUID(user.id)
    credential, cfg = await _credential_target_config(repo, uid, credential_id)
    adapter = _adapter_or_400(credential.adapter)

    options_fn = getattr(adapter, "config_options", None)
    if not callable(options_fn):
        raise HTTPException(
            status_code=501,
            detail=f"Adapter {credential.adapter!r} does not enumerate field options",
        )
    try:
        options = await _call_adapter_within_budget(
            options_fn(field, cfg), adapter=credential.adapter,
            what=f"config_options({field})",
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail=f"{credential.adapter}: timed out listing {field}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - network, auth, plugin bug
        # Reported, not silently degraded (Victor, 2026-08-05): an
        # unreachable system is rare enough that building a free-text
        # fallback around it costs more than it saves, and a picker that
        # quietly turns into a text box hides WHY it did.
        logging.getLogger(__name__).info(
            "adapter %r could not list options for %r: %s", credential.adapter, field, exc
        )
        raise HTTPException(
            status_code=502,
            detail=f"{credential.adapter}: could not list {field} — {str(exc)[:200]}",
        ) from exc

    return DeliveryOptionsOut(options=[
        DeliveryOptionOut(value=str(o.value), label=str(o.label)) for o in options
    ])


@router.delete("/api/delivery-credentials/{credential_id}", status_code=204)
async def delete_delivery_credential_endpoint(
    credential_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    uid = uuid.UUID(user.id)
    if await repo.get_delivery_credential(uid, credential_id) is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    # Checked before deleting so the caller gets a count instead of the
    # RESTRICT foreign key firing as a 500.
    used_by = await repo.count_targets_for_credential(uid, credential_id)
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"Credential is used by {used_by} delivery target(s)",
        )
    await repo.delete_delivery_credential(uid, credential_id)
    await session.commit()
    return Response(status_code=204)


@router.post("/api/delivery-targets", response_model=DeliveryTargetOut)
async def create_delivery_target_endpoint(
    payload: DeliveryTargetCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryTargetOut:
    adapter = _adapter_or_400(payload.adapter)
    repo = Repo(session)
    uid = uuid.UUID(user.id)
    credential = await _credential_for_target_or_400(
        repo, uid, payload.credential_id, payload.adapter
    )
    _validate_merged_or_422(adapter, credential, payload.config)
    row = await repo.create_delivery_target(
        uid,
        name=payload.name.strip(),
        adapter=payload.adapter,
        credential_id=credential.id,
        config=payload.config,
    )
    await session.commit()
    return _delivery_target_out(row)


@router.get("/api/delivery-targets/{target_id}", response_model=DeliveryTargetOut)
async def get_delivery_target_endpoint(
    target_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryTargetOut:
    repo = Repo(session)
    row = await repo.get_delivery_target(uuid.UUID(user.id), target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery target not found")
    return _delivery_target_out(row)


@router.put("/api/delivery-targets/{target_id}", response_model=DeliveryTargetOut)
async def update_delivery_target_endpoint(
    target_id: uuid.UUID,
    payload: DeliveryTargetUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> DeliveryTargetOut:
    repo = Repo(session)
    uid = uuid.UUID(user.id)
    existing = await repo.get_delivery_target(uid, target_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Delivery target not found")

    adapter = _adapter_or_400(existing.adapter)
    credential = await _credential_for_target_or_400(
        repo, uid,
        payload.credential_id or existing.credential_id,
        existing.adapter,
    )
    # Validate against what the target WILL be, not what it was: an update
    # that only moves the target to another credential still has to produce
    # a config the adapter accepts.
    _validate_merged_or_422(
        adapter, credential,
        payload.config if payload.config is not None else existing.config_json,
    )

    row = await repo.update_delivery_target(
        uid, target_id,
        name=payload.name,
        config=payload.config,
        credential_id=credential.id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery target not found")
    await session.commit()
    return _delivery_target_out(row)


@router.delete("/api/delivery-targets/{target_id}", status_code=204)
async def delete_delivery_target_endpoint(
    target_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    if not await repo.delete_delivery_target(uuid.UUID(user.id), target_id):
        raise HTTPException(status_code=404, detail="Delivery target not found")
    await session.commit()
    return Response(status_code=204)


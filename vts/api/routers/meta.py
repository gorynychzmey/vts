"""Account, personalisation and app-metadata endpoints.

The small stuff that belongs to a user rather than to a task: API tokens,
custom prompts and presets, the default preset, push subscriptions, progress
weights, plus the read-only app facts (`/api/version`, `/api/status-config`,
`/api/me`) and the admin user list.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged.

No `tags=` on the router: `_install_custom_openapi()` in `vts.api.main`
derives the OpenAPI tag from the URL prefix, and an explicit tag overrides it
(`/api/admin/users` must stay tagged "admin", not "meta").
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vts import __version__
from vts.api.csrf import require_same_site
from vts.api._helpers.pages_assets import NO_CACHE_HEADERS
from vts.api.deps import (
    get_current_user,
    get_current_user_session_only,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    AdminUsersOut,
    ApiTokenCreateOut,
    ApiTokenCreateRequest,
    ApiTokenOut,
    MeOut,
    PresetCreateRequest,
    PresetOptions,
    PresetOut,
    PresetRef,
    PresetUpdateRequest,
    ProgressWeightsOut,
    PromptCreateRequest,
    PromptDetailOut,
    PromptOut,
    PromptUpdateRequest,
    PushConfigOut,
    PushStatusOut,
    PushSubscriptionIn,
    PushUnsubscribeIn,
    SystemPromptTextOut,
)
from vts.core.config import Settings
from vts.db.repo import Repo
from vts.metrics.step_weights import SEED_FINAL_SUMMARY_FALLBACK, SEED_STEP_WEIGHTS
from vts.services import task_status as _ts
from vts.services.auth import AuthenticatedUser
from vts.services.push import (
    SubscriptionPayload,
    delete_subscription,
    is_push_enabled,
    list_subscriptions,
    upsert_subscription,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/version")
async def version() -> JSONResponse:
    return JSONResponse({"version": __version__}, headers=NO_CACHE_HEADERS)


@router.get("/api/status-config")
async def status_config(
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Pure-status semantics for the frontend, fetched once at bootstrap.
    Task-DEPENDENT capabilities ride per-task on TaskOut.capabilities."""
    return JSONResponse(
        {
            "status_flags": _ts.status_flags(),
            "tasks_page_size": settings.tasks_page_size,
        },
        headers=NO_CACHE_HEADERS,
    )


@router.get("/api/me", response_model=MeOut)
async def me(user: AuthenticatedUser = Depends(get_current_user)) -> MeOut:
    return MeOut(requested_by=user.requested_by, acting_as=user.acting_as, is_admin=user.is_admin)


@router.get("/api/me/tokens", response_model=list[ApiTokenOut], include_in_schema=False)
async def list_tokens(
    user: AuthenticatedUser = Depends(get_current_user_session_only),
    session: AsyncSession = Depends(get_session_dep),
) -> list[ApiTokenOut]:
    from vts.db.repo import Repo as _Repo
    repo = _Repo(session)
    rows = await repo.list_api_tokens(uuid.UUID(user.id))
    return [
        ApiTokenOut(
            id=r.id, name=r.name, prefix=r.prefix,
            created_at=r.created_at, last_used_at=r.last_used_at,
        )
        for r in rows
    ]


@router.post(
    "/api/me/tokens",
    response_model=ApiTokenCreateOut,
    dependencies=[Depends(require_same_site)],
    include_in_schema=False,
)
async def create_token(
    payload: ApiTokenCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user_session_only),
    session: AsyncSession = Depends(get_session_dep),
) -> ApiTokenCreateOut:
    from vts.db.repo import Repo as _Repo
    from vts.services.api_tokens import generate_token, hash_token, token_prefix
    raw = generate_token()
    repo = _Repo(session)
    row = await repo.create_api_token(
        user_id=uuid.UUID(user.id),
        name=payload.name.strip(),
        token_hash=hash_token(raw),
        prefix=token_prefix(raw),
    )
    await session.commit()
    return ApiTokenCreateOut(
        id=row.id, name=row.name, prefix=row.prefix,
        created_at=row.created_at, last_used_at=None, token=raw,
    )


@router.delete(
    "/api/me/tokens/{token_id}",
    status_code=204,
    dependencies=[Depends(require_same_site)],
    include_in_schema=False,
)
async def revoke_token(
    token_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user_session_only),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    from vts.db.repo import Repo as _Repo
    repo = _Repo(session)
    ok = await repo.revoke_api_token(uuid.UUID(user.id), token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/api/prompts", response_model=list[PromptOut])
async def list_prompts_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[PromptOut]:
    # The vendor prompt is no longer listed separately: it is one of the user's
    # own rows now, flagged `is_system` (vts-kujy).
    repo = Repo(session)
    out: list[PromptOut] = []
    for row in await repo.list_prompts(uuid.UUID(user.id)):
        out.append(
            PromptOut(
                source="user",
                id=str(row.id),
                name=row.name,
                editable=True,
                is_system=row.is_system,
            )
        )
    return out


@router.post("/api/prompts", response_model=PromptOut)
async def create_prompt_endpoint(
    payload: PromptCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PromptOut:
    repo = Repo(session)
    row = await repo.create_prompt(uuid.UUID(user.id), payload.name.strip(), payload.system_prompt)
    await session.commit()
    return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)


@router.get("/api/prompts/system/{key}/text", response_model=SystemPromptTextOut)
async def get_system_prompt_text_endpoint(
    key: str,
    user: AuthenticatedUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
    session: AsyncSession = Depends(get_session_dep),
) -> SystemPromptTextOut:
    # Serves the user's own copy, creating it on first read (vts-kujy).
    from vts.services.system_prompt import get_or_create_system_prompt

    if key != "summary":
        raise HTTPException(status_code=404, detail="System prompt not found")
    prompt = await get_or_create_system_prompt(
        session, uuid.UUID(user.id), settings.prompts_dir
    )
    await session.commit()
    return SystemPromptTextOut(system_prompt=prompt.system_prompt)


@router.get("/api/prompts/{prompt_id}", response_model=PromptDetailOut)
async def get_prompt_detail_endpoint(
    prompt_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PromptDetailOut:
    repo = Repo(session)
    row = await repo.get_prompt(uuid.UUID(user.id), prompt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return PromptDetailOut(
        source="user",
        id=str(row.id),
        name=row.name,
        system_prompt=row.system_prompt,
        editable=True,
    )


@router.patch("/api/prompts/{prompt_id}", response_model=PromptOut)
async def update_prompt_endpoint(
    prompt_id: uuid.UUID,
    payload: PromptUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PromptOut:
    repo = Repo(session)
    row = await repo.update_prompt(
        uuid.UUID(user.id), prompt_id,
        name=payload.name,  # validated + stripped by PromptUpdateRequest; None = unchanged
        system_prompt=payload.system_prompt,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    await session.commit()
    return PromptOut(source="user", id=str(row.id), name=row.name, editable=True)


@router.delete("/api/prompts/{prompt_id}", status_code=204)
async def delete_prompt_endpoint(
    prompt_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    ok = await repo.delete_prompt(uuid.UUID(user.id), prompt_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Prompt not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/api/presets", response_model=list[PresetOut])
async def list_presets_endpoint(user: AuthenticatedUser = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session_dep)) -> list[PresetOut]:
    from vts.services.preset_registry import list_system_presets
    out = [PresetOut(source="system", id=p.key, name=p.display_name,
                     options=PresetOptions(**p.options), editable=False)
           for p in list_system_presets()]
    repo = Repo(session)
    for row in await repo.list_presets(uuid.UUID(user.id)):
        out.append(PresetOut(source="user", id=str(row.id), name=row.name,
                             options=PresetOptions(**row.options), editable=True))
    return out


@router.post("/api/presets", response_model=PresetOut)
async def create_preset_endpoint(payload: PresetCreateRequest,
                                 user: AuthenticatedUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session_dep)) -> PresetOut:
    repo = Repo(session)
    row = await repo.create_preset(uuid.UUID(user.id), payload.name.strip(), payload.options.model_dump())
    await session.commit()
    return PresetOut(source="user", id=str(row.id), name=row.name,
                     options=PresetOptions(**row.options), editable=True)


@router.patch("/api/presets/{preset_id}", response_model=PresetOut)
async def update_preset_endpoint(preset_id: uuid.UUID, payload: PresetUpdateRequest,
                                 user: AuthenticatedUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session_dep)) -> PresetOut:
    repo = Repo(session)
    row = await repo.update_preset(uuid.UUID(user.id), preset_id,
                                   name=payload.name,
                                   options=payload.options.model_dump() if payload.options else None)
    if row is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    await session.commit()
    return PresetOut(source="user", id=str(row.id), name=row.name,
                     options=PresetOptions(**row.options), editable=True)


@router.delete("/api/presets/{preset_id}", status_code=204)
async def delete_preset_endpoint(preset_id: uuid.UUID,
                                 user: AuthenticatedUser = Depends(get_current_user),
                                 session: AsyncSession = Depends(get_session_dep)) -> Response:
    repo = Repo(session)
    if not await repo.delete_preset(uuid.UUID(user.id), preset_id):
        raise HTTPException(status_code=404, detail="Preset not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/api/me/default_preset")
async def get_default_preset_endpoint(user: AuthenticatedUser = Depends(get_current_user),
                                      session: AsyncSession = Depends(get_session_dep)) -> dict:
    from vts.services.preset_registry import default_system_preset
    repo = Repo(session)
    ref = await repo.get_user_default_preset(uuid.UUID(user.id))
    return ref or {"source": "system", "id": default_system_preset().key}


@router.put("/api/me/default_preset", status_code=204)
async def set_default_preset_endpoint(payload: PresetRef,
                                      user: AuthenticatedUser = Depends(get_current_user),
                                      session: AsyncSession = Depends(get_session_dep)) -> Response:
    from vts.services.preset_registry import system_preset_keys
    repo = Repo(session)
    if payload.source == "system":
        if payload.id not in system_preset_keys():
            raise HTTPException(status_code=404, detail="Unknown system preset")
    else:
        if await repo.get_preset(uuid.UUID(user.id), uuid.UUID(payload.id)) is None:
            raise HTTPException(status_code=404, detail="Preset not found")
    await repo.set_user_default_preset(uuid.UUID(user.id), {"source": payload.source, "id": payload.id})
    await session.commit()
    return Response(status_code=204)


@router.get("/api/progress-weights", response_model=ProgressWeightsOut)
async def progress_weights_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> ProgressWeightsOut:
    repo = Repo(session)
    row = await repo.get_user_step_weights(uuid.UUID(user.id))
    if row is not None and isinstance(row.weights, dict) and row.weights:
        fallback = row.final_summary_fallback
        return ProgressWeightsOut(
            weights={k: float(v) for k, v in row.weights.items()},
            final_summary_fallback=float(fallback) if fallback is not None else SEED_FINAL_SUMMARY_FALLBACK,
        )
    return ProgressWeightsOut(
        weights=dict(SEED_STEP_WEIGHTS),
        final_summary_fallback=SEED_FINAL_SUMMARY_FALLBACK,
    )


@router.get("/api/push/config", response_model=PushConfigOut, include_in_schema=False)
async def push_config(settings: Settings = Depends(get_settings_dep)) -> PushConfigOut:
    if not is_push_enabled(settings):
        return PushConfigOut(enabled=False, public_key=None)
    return PushConfigOut(enabled=True, public_key=settings.vapid_public_key)


@router.get("/api/push/status", response_model=PushStatusOut, include_in_schema=False)
async def push_status(
    endpoint: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PushStatusOut:
    subs = await list_subscriptions(session, uuid.UUID(user.id))
    if endpoint:
        match = next((s for s in subs if s.endpoint == endpoint), None)
        return PushStatusOut(subscribed=match is not None, endpoint=endpoint if match else None)
    first = subs[0] if subs else None
    return PushStatusOut(subscribed=first is not None, endpoint=first.endpoint if first else None)


@router.post("/api/push/subscribe", response_model=PushStatusOut, include_in_schema=False)
async def push_subscribe(
    payload: PushSubscriptionIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    settings: Settings = Depends(get_settings_dep),
) -> PushStatusOut:
    if not is_push_enabled(settings):
        raise HTTPException(status_code=503, detail="Push notifications are not configured")
    await upsert_subscription(
        session,
        uuid.UUID(user.id),
        SubscriptionPayload(
            endpoint=payload.endpoint,
            p256dh=payload.p256dh,
            auth=payload.auth,
            user_agent=payload.user_agent,
        ),
    )
    return PushStatusOut(subscribed=True, endpoint=payload.endpoint)


@router.post("/api/push/unsubscribe", response_model=PushStatusOut, include_in_schema=False)
async def push_unsubscribe(
    payload: PushUnsubscribeIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> PushStatusOut:
    await delete_subscription(session, payload.endpoint)
    return PushStatusOut(subscribed=False, endpoint=None)


@router.get("/api/admin/users", response_model=AdminUsersOut)
async def admin_users(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUsersOut:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    repo = Repo(session)
    users = await repo.list_usernames()
    return AdminUsersOut(users=users)

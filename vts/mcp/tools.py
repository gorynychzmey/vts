from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from vts.db.models import TaskStatus
from vts.mcp.cursor import decode_cursor, encode_cursor
from vts.mcp.schemas import (
    DeliveryStatusInfo,
    DeliveryCredentialInfo,
    DeliveryTargetInfo,
    PresetInfo,
    ProgressCounts,
    PromptInfo,
    PromptResult,
    SubmitVideoResult,
    TaskPage,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.services import task_status
from vts.services.delivery_submit import DeliveryValidationError, validate_delivery_refs
from vts.services.preset_expand import expand_preset_options, resolve_preset
from vts.services.preset_registry import (
    default_system_preset,
    list_system_presets,
    parse_preset_ref,
    system_preset_keys,
)
from vts.services.prompt_registry import list_system_prompts, parse_ref, ref_to_dict
from vts.services.source_url import InvalidSourceUrl, validate_source_url
from vts.services.prompt_results import resolve_result_path
from vts.services.storage import task_dir
from vts.services.task_progress import summary_progress_for_task


class _UserLike(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def username(self) -> str: ...


class _RepoLike(Protocol):
    async def create_task(
        self,
        user_id: uuid.UUID,
        source_url: str,
        options: dict[str, Any],
        artifact_dir: str,
        task_id: uuid.UUID | None = None,
    ) -> Any: ...

    async def get_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> Any | None: ...
    async def list_prompts(self, user_id: uuid.UUID) -> list[Any]: ...
    async def get_delivery_target_by_name(self, user_id: uuid.UUID, name: str) -> Any | None: ...


class _BusLike(Protocol):
    async def notify_queued(self) -> None: ...

    async def publish_event(
        self,
        *,
        user_id: str,
        task_id: str,
        event: str,
        data: dict[str, Any],
        throttle_key: str | None = None,
    ) -> None: ...


async def submit_video(
    *,
    url: str,
    user: _UserLike,
    repo: _RepoLike,
    bus: _BusLike,
    artifacts_root: Path,
    language: str | None = None,
    audio_only: bool = False,
    transcript: bool = True,
    diarize: bool = False,
    prompts: list[dict] | None = None,
    preset: dict | None = None,
    delivery: list[dict] | None = None,
) -> SubmitVideoResult:
    """Create a new task in the queued state and notify the worker.

    Pipeline options mirror web /api/tasks (VOS-63) so a bare URL submit
    runs the full transcript+summary pipeline by default. `prompts` defaults
    to the single system "summary" prompt; non-empty prompts require
    `transcript=True` — the worker would otherwise have nothing to run
    prompts against. `diarize` defaults to False (it costs a full extra pass
    over the audio) and likewise requires `transcript=True` — there is
    nothing to attribute speakers to without a transcript.

    `preset` (a ref like {"source": "system", "id": "default"} or
    {"source": "user", "id": "<uuid>"}) supplies default pipeline options.
    When given, the preset's options form the base; explicit caller params
    override the base ONLY for fields the caller left at their default —
    i.e. the preset fills the fields you didn't set:
      - language: caller wins if `language is not None`, else preset's.
      - audio_only: caller wins if `audio_only is True` (non-default),
        else preset's.
      - transcript: caller wins if `transcript is False` (non-default),
        else preset's.
      - diarize: caller wins if `diarize is True` (non-default), else
        preset's.
      - prompts: caller wins if `prompts is not None`, else preset's.
      - delivery: caller wins if `delivery is not None`, else preset's.
    With no preset, behaviour is unchanged.

    `delivery` is a list of `{deliver_to: "<target name>", variant?}`. An
    EXPLICIT delivery is validated here: an unknown target name, or a target
    whose adapter plugin is not currently loaded, raises 422 so the caller finds
    out at submit time. Delivery inherited from a PRESET is not gated that way —
    a temporarily missing plugin must not fail the task; that delivery is
    enqueued and parked until the adapter returns.
    """
    if not url or not url.strip():
        raise HTTPException(status_code=422, detail="url is required")
    # This path does NOT build a TaskCreateRequest, so it needs its own call to
    # the shared validator — validating only the Pydantic schema would leave
    # MCP as an open server-side request primitive (vts-h45).
    try:
        url = validate_source_url(url)
    except InvalidSourceUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Delivery inherited from a preset (if any). Kept separate from an explicit
    # `delivery` because only the explicit one is validated up-front: a preset
    # naming a target whose plugin is missing must park, not fail the submit.
    preset_delivery: list[dict] = []
    if preset is None:
        if prompts is None:
            norm: list[dict] = [ref_to_dict("system", "summary")]
        else:
            norm = []
            for entry in prompts:
                try:
                    source, ref_id = parse_ref(entry)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                norm.append(ref_to_dict(source, ref_id))
    else:
        try:
            p_source, p_id = parse_preset_ref(preset)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user_preset_options: dict | None = None
        if p_source == "user":
            row = await repo.get_preset(uuid.UUID(user.id), uuid.UUID(p_id))
            if row is None:
                raise HTTPException(status_code=404, detail="Preset not found")
            user_preset_options = row.options
        resolved = resolve_preset(p_source, p_id, list_system_presets(), user_preset_options)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Unknown system preset")
        valid_user_prompt_ids = {str(p.id) for p in await repo.list_prompts(uuid.UUID(user.id))}
        base = expand_preset_options(resolved, valid_user_prompt_ids)
        # Preset fills fields the caller left at default; explicit non-default
        # caller params override.
        if language is None:
            language = base["language"]
        if audio_only is False:
            audio_only = bool(base["audio_only"])
        if transcript is True:
            transcript = bool(base["transcript"])
        if diarize is False:
            diarize = bool(base["diarize"])
        if prompts is None:
            norm = list(base["prompts"])
        else:
            norm = []
            for entry in prompts:
                try:
                    source, ref_id = parse_ref(entry)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                norm.append(ref_to_dict(source, ref_id))
        # Field-level replace, like every other option: an explicit `delivery`
        # wins outright, otherwise the preset's applies.
        if delivery is None:
            preset_delivery = list(base.get("delivery", []))
    if norm and not transcript:
        raise HTTPException(status_code=422, detail="prompts require transcript")
    if diarize and not transcript:
        raise HTTPException(status_code=422, detail="diarize requires transcript")

    if delivery is None:
        delivery_refs = preset_delivery
    else:
        # Explicit submit: unknown target or unavailable adapter fails NOW.
        try:
            delivery_refs = await validate_delivery_refs(
                repo, uuid.UUID(user.id), delivery
            )
        except DeliveryValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(d.get("variant") == "summary" for d in delivery_refs) and not norm:
        raise HTTPException(
            status_code=422, detail="delivery variant 'summary' requires prompts"
        )
    # Same rule as the REST path: delivering a prompt's result requires that
    # prompt to run, or the delivery waits forever on an artifact nothing
    # produces (vts-as1i).
    selected_refs = {f"{p.get('source')}:{p.get('id')}" for p in norm}
    for entry in delivery_refs:
        variant = str(entry.get("variant") or "")
        if ":" not in variant:
            continue
        if variant not in selected_refs:
            raise HTTPException(
                status_code=422,
                detail=f"delivery variant {variant!r} needs that prompt selected",
            )

    task_id = uuid.uuid4()
    artifact = task_dir(artifacts_root, user.username, task_id)
    artifact.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "language": language,
        "audio_only": audio_only,
        "transcript": transcript,
        "diarize": diarize,
        "prompts": norm,
        "delivery": delivery_refs,
    }
    task = await repo.create_task(
        user_id=uuid.UUID(user.id),
        source_url=url.strip(),
        options=options,
        artifact_dir=str(artifact),
        task_id=task_id,
    )
    await bus.notify_queued()
    await bus.publish_event(
        user_id=str(task.user_id),
        task_id=str(task.id),
        event="task_status",
        data={"status": str(task.status)},
    )
    return SubmitVideoResult(task_id=task.id, status=task.status, created_at=task.created_at)


class _RepoListLike(Protocol):
    async def list_tasks_page(
        self,
        user_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        order: str = "desc",
        limit: int = 20,
        status: Any = None,
        q: str | None = None,
        created_from: Any = None,
        created_to: Any = None,
        source_type: str | None = None,
    ) -> list[Any]: ...


async def list_tasks(
    *,
    user: _UserLike,
    repo: _RepoListLike,
    status: Literal["queued", "running", "waiting", "paused", "completed", "archived", "failed", "canceled"] | None = None,
    limit: int = 20,
    cursor: str | None = None,
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    source_type: Literal["file", "url"] | None = None,
) -> TaskPage:
    """List the caller's tasks newest-first, one page at a time.

    Pages on the immutable ``created_at`` cursor. Pass ``cursor`` (the
    ``next_cursor`` from a prior page) to fetch the next page. ``status``,
    ``q`` (matches title or URL), ``created_from``/``created_to`` and
    ``source_type`` optionally narrow the set; keep them identical across
    pages, since changing a filter changes what the cursor points into.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=422, detail="created_from must not be after created_to"
        )
    before = None
    if cursor:
        try:
            before = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
    status_enum = TaskStatus(status) if status is not None else None
    tasks = await repo.list_tasks_page(
        uuid.UUID(user.id),
        before=before,
        order="desc",
        limit=limit,
        status=status_enum,
        q=q,
        created_from=created_from,
        created_to=created_to,
        source_type=source_type,
    )
    summaries = [
        TaskSummary(
            task_id=t.id,
            status=t.status,
            title=t.source_title,
            url=t.source_url,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in tasks
    ]
    has_more = len(tasks) == limit
    next_cursor = (
        encode_cursor(tasks[-1].created_at, tasks[-1].id)
        if (has_more and tasks)
        else None
    )
    return TaskPage(tasks=summaries, next_cursor=next_cursor, has_more=has_more)


def _stage_label(task: Any) -> str | None:
    """Return the name of the first running step, or None."""
    steps = getattr(task, "steps", None) or []
    for step in steps:
        if str(step.status) == "running":
            return step.name
    return None


class _RepoStatusLike(Protocol):
    async def get_task_for_user(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Any | None: ...
    async def get_asr_progress_for_tasks(
        self, task_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]: ...


_ASR_STAGE = "transcribe_segments"
_SUMMARY_STAGES = frozenset({"summarize_windows", "pack_window_notes", "summarize_final"})


def _progress_for_stage(
    stage: str | None,
    task: Any,
    asr_map: dict[uuid.UUID, tuple[int, int]],
) -> ProgressCounts | None:
    """Return the progress counter for the currently active stage, or None."""
    if stage is None:
        return None
    if stage == _ASR_STAGE:
        current, total = asr_map.get(task.id, (0, 0))
        return ProgressCounts(current=current, total=total)
    if stage in _SUMMARY_STAGES or stage.startswith("finalize:"):
        current, total = summary_progress_for_task(task)
        return ProgressCounts(current=current, total=total)
    return None


async def get_status(
    *,
    task_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoStatusLike,
) -> TaskStatusResult:
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    asr_map = await repo.get_asr_progress_for_tasks([task.id])
    stage = _stage_label(task)
    return TaskStatusResult(
        task_id=task.id,
        status=str(task.status),
        stage=stage,
        progress=_progress_for_stage(stage, task, asr_map),
        error=task.error_message,
        updated_at=task.updated_at,
    )


async def get_prompt_result(
    *,
    task_id: uuid.UUID,
    ref: str,
    user: _UserLike,
    repo: _RepoStatusLike,
) -> PromptResult:
    """Fetch the rendered text for one prompt result of a task.

    ``ref`` is a "source:id" string (e.g. "system:summary" or
    "user:<uuid>"). 404 when the task is unknown or the result is missing.
    """
    try:
        source, ref_id = parse_ref(ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    path = resolve_result_path(task, source, ref_id)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Result not found")
    return PromptResult(
        task_id=task.id,
        source=source,
        id=ref_id,
        content=Path(path).read_text(encoding="utf-8"),
    )


class _RepoDeliveryLike(Protocol):
    async def create_delivery_credential(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        config: dict, secrets_enc: bytes | None,
    ) -> Any: ...
    async def list_delivery_credentials(self, user_id: uuid.UUID) -> list[Any]: ...
    async def get_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> Any | None: ...
    async def count_targets_for_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> int: ...
    async def update_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        secrets_enc: bytes | None, clear_secrets: bool,
    ) -> Any | None: ...
    async def delete_delivery_credential(
        self, user_id: uuid.UUID, credential_id: uuid.UUID
    ) -> bool: ...
    async def create_delivery_target(
        self, user_id: uuid.UUID, *, name: str, adapter: str,
        credential_id: uuid.UUID, config: dict,
    ) -> Any: ...
    async def list_delivery_targets(self, user_id: uuid.UUID) -> list[Any]: ...
    async def get_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> Any | None: ...
    async def update_delivery_target(
        self, user_id: uuid.UUID, target_id: uuid.UUID, *,
        name: str | None, config: dict | None,
        credential_id: uuid.UUID | None,
    ) -> Any | None: ...
    async def delete_delivery_target(self, user_id: uuid.UUID, target_id: uuid.UUID) -> bool: ...


def _encrypt_or_400(secrets: dict[str, str], settings: Any) -> bytes:
    from vts.core.secrets import SecretsKeyMissing, encrypt_secrets, load_secrets_key

    try:
        return encrypt_secrets(secrets, load_secrets_key(settings))
    except SecretsKeyMissing as exc:
        raise HTTPException(
            status_code=400,
            detail="VTS_SECRETS_KEY is not configured; cannot store delivery secrets",
        ) from exc


def _adapter_or_400(adapter: str):
    from vts.delivery.registry import UnknownAdapter, get_adapter

    try:
        return get_adapter(adapter)
    except UnknownAdapter as exc:
        raise HTTPException(
            status_code=400, detail=f"Unknown delivery adapter: {adapter}"
        ) from exc


def _uuid_or_422(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a UUID") from exc


async def create_delivery_credential(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any,
    name: str, adapter: str, config: dict | None = None,
    secrets: dict[str, str] | None = None,
) -> DeliveryCredentialInfo:
    """Create a connection. Secrets are encrypted at rest and never returned."""
    from vts.services.delivery_submit import delivery_credential_view

    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    _adapter_or_400(adapter)
    secrets_enc = _encrypt_or_400(secrets, settings) if secrets else None
    row = await repo.create_delivery_credential(
        uuid.UUID(user.id), name=name.strip(), adapter=adapter,
        config=config or {}, secrets_enc=secrets_enc,
    )
    return DeliveryCredentialInfo(**delivery_credential_view(row, settings))


async def list_delivery_credentials(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any
) -> list[DeliveryCredentialInfo]:
    """List the caller's connections. Secret values are never included."""
    from vts.services.delivery_submit import delivery_credential_view

    uid = uuid.UUID(user.id)
    rows = await repo.list_delivery_credentials(uid)
    out = []
    for row in rows:
        used_by = await repo.count_targets_for_credential(uid, row.id)
        out.append(DeliveryCredentialInfo(
            **delivery_credential_view(row, settings, used_by=used_by)
        ))
    return out


async def update_delivery_credential(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any,
    credential_id: str, name: str | None = None, config: dict | None = None,
    secrets: dict[str, str] | None = None, clear_secrets: bool = False,
) -> DeliveryCredentialInfo:
    """Update a connection. Omitting `secrets` keeps the stored ones."""
    from vts.services.delivery_submit import delivery_credential_view

    secrets_enc = _encrypt_or_400(secrets, settings) if secrets else None
    uid = uuid.UUID(user.id)
    cid = _uuid_or_422(credential_id, "credential_id")
    row = await repo.update_delivery_credential(
        uid, cid, name=name, config=config,
        secrets_enc=secrets_enc, clear_secrets=clear_secrets,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    used_by = await repo.count_targets_for_credential(uid, row.id)
    return DeliveryCredentialInfo(
        **delivery_credential_view(row, settings, used_by=used_by)
    )


async def delete_delivery_credential(
    *, user: _UserLike, repo: _RepoDeliveryLike, credential_id: str
) -> dict[str, Any]:
    """Delete a connection, unless targets still reference it."""
    uid = uuid.UUID(user.id)
    cid = _uuid_or_422(credential_id, "credential_id")
    if await repo.get_delivery_credential(uid, cid) is None:
        raise HTTPException(status_code=404, detail="Delivery credential not found")
    used_by = await repo.count_targets_for_credential(uid, cid)
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"Credential is used by {used_by} delivery target(s)",
        )
    await repo.delete_delivery_credential(uid, cid)
    return {"deleted": True, "id": str(cid)}


async def _resolve_credential_or_error(
    repo: _RepoDeliveryLike, uid: uuid.UUID, credential_id: str, adapter: str
) -> Any:
    """Resolve a target's credential, rejecting a missing or mismatched one.

    Checked here rather than left to the foreign key so the agent is told what
    is wrong instead of receiving an integrity error.
    """
    cid = _uuid_or_422(credential_id, "credential_id")
    credential = await repo.get_delivery_credential(uid, cid)
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


def _validate_merged_or_422(adapter: Any, credential: Any, config: dict | None) -> None:
    from vts.services.delivery_config import DeliveryConfigInvalid, validate_config

    merged = dict((credential.config_json or {}) if credential else {})
    merged.update(config or {})
    try:
        validate_config(adapter, merged)
    except DeliveryConfigInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def create_delivery_target(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any,
    name: str, adapter: str, credential_id: str, config: dict | None = None,
) -> DeliveryTargetInfo:
    """Create a delivery target hanging off an existing connection."""
    from vts.services.delivery_submit import delivery_target_view

    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="name is required")
    adapter_obj = _adapter_or_400(adapter)
    uid = uuid.UUID(user.id)
    credential = await _resolve_credential_or_error(repo, uid, credential_id, adapter)
    _validate_merged_or_422(adapter_obj, credential, config)

    row = await repo.create_delivery_target(
        uid, name=name.strip(), adapter=adapter,
        credential_id=credential.id, config=config or {},
    )
    return DeliveryTargetInfo(**delivery_target_view(row, settings))


async def list_delivery_targets(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any
) -> list[DeliveryTargetInfo]:
    """List the caller's delivery targets."""
    from vts.services.delivery_submit import delivery_target_view

    rows = await repo.list_delivery_targets(uuid.UUID(user.id))
    return [DeliveryTargetInfo(**delivery_target_view(r, settings)) for r in rows]


async def update_delivery_target(
    *, user: _UserLike, repo: _RepoDeliveryLike, settings: Any,
    target_id: str, name: str | None = None, config: dict | None = None,
    credential_id: str | None = None,
) -> DeliveryTargetInfo:
    """Update a delivery target."""
    from vts.services.delivery_submit import delivery_target_view

    uid = uuid.UUID(user.id)
    tid = _uuid_or_422(target_id, "target_id")
    existing = await repo.get_delivery_target(uid, tid)
    if existing is None:
        raise HTTPException(status_code=404, detail="Delivery target not found")

    adapter_obj = _adapter_or_400(existing.adapter)
    credential = await _resolve_credential_or_error(
        repo, uid, str(credential_id or existing.credential_id), existing.adapter
    )
    # Validate what the target WILL be: moving it to another credential must
    # still leave the adapter with a config it accepts.
    _validate_merged_or_422(
        adapter_obj, credential,
        config if config is not None else existing.config_json,
    )

    row = await repo.update_delivery_target(
        uid, tid, name=name, config=config, credential_id=credential.id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Delivery target not found")
    return DeliveryTargetInfo(**delivery_target_view(row, settings))


async def delete_delivery_target(
    *, user: _UserLike, repo: _RepoDeliveryLike, target_id: str
) -> dict:
    try:
        tid = uuid.UUID(target_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="target_id must be a UUID") from exc
    if not await repo.delete_delivery_target(uuid.UUID(user.id), tid):
        raise HTTPException(status_code=404, detail="Delivery target not found")
    return {"deleted": True}


class _RepoDeliveryStatusLike(Protocol):
    async def get_task_for_user(self, user_id: uuid.UUID, task_id: uuid.UUID) -> Any: ...
    async def list_deliveries_for_task(self, task_id: uuid.UUID) -> list[Any]: ...
    async def reset_delivery_for_retry(
        self, task_id: uuid.UUID, target_id: uuid.UUID | None, now: Any
    ) -> int: ...


async def get_delivery_status(
    *, user: _UserLike, repo: _RepoDeliveryStatusLike, task_id: uuid.UUID
) -> list[DeliveryStatusInfo]:
    """Deliveries of one task: where each one got to, and why if it stalled."""
    from vts.services.delivery_status import is_waiting_for_adapter

    await repo.get_task_for_user(uuid.UUID(user.id), task_id)  # 404s if not owned
    rows = await repo.list_deliveries_for_task(task_id)
    return [
        DeliveryStatusInfo(
            id=str(r.id), adapter=r.adapter, variant=r.variant,
            status=str(r.status), attempts=r.attempts, max_attempts=r.max_attempts,
            last_error=r.last_error, external_url=r.external_url,
            waiting_for_adapter=is_waiting_for_adapter(r.status),
        )
        for r in rows
    ]


async def retry_delivery(
    *, user: _UserLike, repo: _RepoDeliveryStatusLike, task_id: uuid.UUID,
    target_id: str | None = None,
) -> dict:
    """Revive dead deliveries of a task.

    Rows parked in `waiting_adapter` are deliberately untouched: they are not
    stuck, and forcing them to retry would spend attempts against an adapter
    that is still missing.
    """
    from vts.db.models import utcnow

    await repo.get_task_for_user(uuid.UUID(user.id), task_id)  # 404s if not owned
    tid = None
    if target_id:
        try:
            tid = uuid.UUID(target_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="target_id must be a UUID") from exc
    reset = await repo.reset_delivery_for_retry(task_id, tid, utcnow())
    return {"reset": reset}


class _RepoPromptLike(Protocol):
    async def create_prompt(self, user_id: uuid.UUID, name: str, system_prompt: str) -> Any: ...
    async def list_prompts(self, user_id: uuid.UUID) -> list[Any]: ...
    async def update_prompt(
        self,
        user_id: uuid.UUID,
        prompt_id: uuid.UUID,
        *,
        name: str | None,
        system_prompt: str | None,
    ) -> Any | None: ...
    async def delete_prompt(self, user_id: uuid.UUID, prompt_id: uuid.UUID) -> bool: ...


async def list_prompts(
    *,
    user: _UserLike,
    repo: _RepoPromptLike,
) -> list[PromptInfo]:
    """List prompts available to the caller: built-in system prompts first,
    then the user's own prompts (mirrors web GET /api/prompts)."""
    out: list[PromptInfo] = [
        PromptInfo(source="system", id=p.key, name=p.display_name, editable=False)
        for p in list_system_prompts()
    ]
    for row in await repo.list_prompts(uuid.UUID(user.id)):
        out.append(PromptInfo(source="user", id=str(row.id), name=row.name, editable=True))
    return out


async def create_prompt(
    *,
    name: str,
    system_prompt: str,
    user: _UserLike,
    repo: _RepoPromptLike,
) -> PromptInfo:
    """Create a user-defined prompt. Returns the new prompt's info."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    if not system_prompt:
        raise HTTPException(status_code=422, detail="system_prompt is required")
    row = await repo.create_prompt(uuid.UUID(user.id), name, system_prompt)
    return PromptInfo(source="user", id=str(row.id), name=row.name, editable=True)


async def update_prompt(
    *,
    prompt_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoPromptLike,
    name: str | None = None,
    system_prompt: str | None = None,
) -> PromptInfo:
    """Update a user-defined prompt's name and/or body. 404 if not found."""
    # name is optional (None = leave unchanged), but a provided name must be
    # non-empty after trimming — consistent with create and the HTTP endpoint.
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name must not be blank")
    row = await repo.update_prompt(
        uuid.UUID(user.id),
        prompt_id,
        name=name,
        system_prompt=system_prompt,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return PromptInfo(source="user", id=str(row.id), name=row.name, editable=True)


async def delete_prompt(
    *,
    prompt_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoPromptLike,
) -> dict[str, Any]:
    """Delete a user-defined prompt. 404 if not found."""
    ok = await repo.delete_prompt(uuid.UUID(user.id), prompt_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return {"deleted": True, "id": str(prompt_id)}


class _RepoPresetLike(Protocol):
    async def create_preset(self, user_id: uuid.UUID, name: str, options: dict) -> Any: ...
    async def list_presets(self, user_id: uuid.UUID) -> list[Any]: ...
    async def get_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> Any | None: ...
    async def update_preset(
        self,
        user_id: uuid.UUID,
        preset_id: uuid.UUID,
        *,
        name: str | None,
        options: dict | None,
    ) -> Any | None: ...
    async def delete_preset(self, user_id: uuid.UUID, preset_id: uuid.UUID) -> bool: ...
    async def get_user_default_preset(self, user_id: uuid.UUID) -> dict | None: ...
    async def set_user_default_preset(self, user_id: uuid.UUID, ref: dict | None) -> None: ...


async def list_presets(
    *,
    user: _UserLike,
    repo: _RepoPresetLike,
) -> list[PresetInfo]:
    """List presets available to the caller: built-in system presets first,
    then the user's own presets (mirrors web GET /api/presets)."""
    out: list[PresetInfo] = [
        PresetInfo(source="system", id=p.key, name=p.display_name, editable=False, options=dict(p.options))
        for p in list_system_presets()
    ]
    for row in await repo.list_presets(uuid.UUID(user.id)):
        out.append(
            PresetInfo(source="user", id=str(row.id), name=row.name, editable=True, options=dict(row.options))
        )
    return out


async def create_preset(
    *,
    name: str,
    options: dict,
    user: _UserLike,
    repo: _RepoPresetLike,
) -> PresetInfo:
    """Create a user-defined preset. Returns the new preset's info."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    row = await repo.create_preset(uuid.UUID(user.id), name, dict(options or {}))
    return PresetInfo(source="user", id=str(row.id), name=row.name, editable=True, options=dict(row.options))


async def update_preset(
    *,
    preset_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoPresetLike,
    name: str | None = None,
    options: dict | None = None,
) -> PresetInfo:
    """Update a user-defined preset's name and/or options. 404 if not found."""
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="name must not be blank")
    row = await repo.update_preset(
        uuid.UUID(user.id),
        preset_id,
        name=name,
        options=dict(options) if options is not None else None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return PresetInfo(source="user", id=str(row.id), name=row.name, editable=True, options=dict(row.options))


async def delete_preset(
    *,
    preset_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoPresetLike,
) -> dict[str, Any]:
    """Delete a user-defined preset. 404 if not found."""
    ok = await repo.delete_preset(uuid.UUID(user.id), preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Preset not found")
    return {"deleted": True, "id": str(preset_id)}


async def get_default_preset(
    *,
    user: _UserLike,
    repo: _RepoPresetLike,
) -> dict[str, Any]:
    """Return the caller's default preset ref, falling back to the system default."""
    ref = await repo.get_user_default_preset(uuid.UUID(user.id))
    return ref or {"source": "system", "id": default_system_preset().key}


async def set_default_preset(
    *,
    source: str,
    id: str,
    user: _UserLike,
    repo: _RepoPresetLike,
) -> dict[str, Any]:
    """Set the caller's default preset. 404 if the referenced preset is unknown."""
    try:
        source, ref_id = parse_preset_ref({"source": source, "id": id})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if source == "system":
        if ref_id not in system_preset_keys():
            raise HTTPException(status_code=404, detail="Unknown system preset")
    else:
        if await repo.get_preset(uuid.UUID(user.id), uuid.UUID(ref_id)) is None:
            raise HTTPException(status_code=404, detail="Preset not found")
    ref = {"source": source, "id": ref_id}
    await repo.set_user_default_preset(uuid.UUID(user.id), ref)
    return ref


async def get_transcript(
    *,
    task_id: uuid.UUID,
    variant: Literal["raw", "redacted"],
    user: _UserLike,
    repo: _RepoStatusLike,
) -> TranscriptResult:
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if variant == "raw":
        if not task.transcript_path:
            raise HTTPException(status_code=404, detail="Transcript is not ready")
        path = Path(task.transcript_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Transcript file missing")
        fmt = "txt" if path.suffix == ".txt" else "json"
    else:  # redacted
        path = Path(task.artifact_dir) / "outputs" / "redacted_transcript.txt"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Redacted transcript is not ready")
        fmt = "txt"
    return TranscriptResult(
        task_id=task.id,
        variant=variant,
        content=path.read_text(encoding="utf-8"),
        format=fmt,
    )


_TERMINAL = {s.value for s in task_status.TERMINAL_FOR_WAIT_STATUSES}
_WAIT_POLL_INTERVAL_SECONDS = 5.0  # seconds between DB re-checks when no event arrives


def _wait_condition_met(task: Any, until: str) -> bool:
    if str(task.status) in _TERMINAL:
        return True
    if until == "transcript":
        return bool(task.transcript_path)
    if until == "summary":
        return bool(task.summary_path)
    return False  # until == "done" already handled by terminal check


def _event_implies_target(event_name: str, data: dict, until: str) -> bool:
    if event_name == "task_status" and data.get("status") in _TERMINAL:
        return True
    if (
        until == "transcript"
        and event_name == "phase"
        and data.get("phase") == "merge_transcript"
        and data.get("status") == "done"
    ):
        return True
    # For until == "summary" there is no dedicated phase event; we rely on
    # the DB re-check on each wake-up (handled by the loop).
    return False


class _PubSubLike(Protocol):
    async def subscribe(self, channel: str) -> None: ...
    async def unsubscribe(self, channel: str | None = None) -> None: ...
    async def close(self) -> None: ...
    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None) -> Any: ...


class _RedisLike(Protocol):
    def pubsub(self) -> _PubSubLike: ...


async def wait_for_task(
    *,
    task_id: uuid.UUID,
    until: str = "done",
    timeout_seconds: int = 300,
    user: _UserLike,
    repo: _RepoStatusLike,
    redis: _RedisLike,
    events_channel: str,
) -> WaitResult:
    if until not in {"transcript", "summary", "done"}:
        raise HTTPException(status_code=422, detail="invalid 'until' value")
    if timeout_seconds < 1 or timeout_seconds > 1800:
        raise HTTPException(status_code=422, detail="timeout_seconds must be 1..1800")

    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(events_channel)
        # subscribe-then-check: any event after `subscribe` is buffered.
        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if _wait_condition_met(task, until):
            return WaitResult(
                task_id=task.id, status=str(task.status), reached=True,
                stage=None, updated_at=task.updated_at,
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=min(remaining, _WAIT_POLL_INTERVAL_SECONDS))
            if not msg:
                # periodic re-check covers the no-phase-for-summary case
                task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
                if task and _wait_condition_met(task, until):
                    return WaitResult(
                        task_id=task.id, status=str(task.status), reached=True,
                        stage=None, updated_at=task.updated_at,
                    )
                continue
            payload = json.loads(msg["data"].decode("utf-8"))
            if payload.get("user_id") != user.id:
                continue
            if payload.get("task_id") != str(task_id):
                continue
            if _event_implies_target(payload.get("event", ""), payload.get("data") or {}, until):
                task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
                return WaitResult(
                    task_id=task.id, status=str(task.status), reached=True,
                    stage=None, updated_at=task.updated_at,
                )

        task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
        return WaitResult(
            task_id=task.id, status=str(task.status), reached=False,
            stage=None, updated_at=task.updated_at,
        )
    finally:
        await pubsub.unsubscribe(events_channel)
        await pubsub.close()

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from vts.mcp.schemas import (
    PresetInfo,
    ProgressCounts,
    PromptInfo,
    PromptResult,
    SubmitVideoResult,
    TaskStatusResult,
    TaskSummary,
    TranscriptResult,
    WaitResult,
)
from vts.services import task_status
from vts.services.preset_expand import expand_preset_options, resolve_preset
from vts.services.preset_registry import (
    default_system_preset,
    list_system_presets,
    parse_preset_ref,
    system_preset_keys,
)
from vts.services.prompt_registry import list_system_prompts, parse_ref, ref_to_dict
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
    With no preset, behaviour is unchanged.
    """
    if not url or not url.strip():
        raise HTTPException(status_code=422, detail="url is required")
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
    if norm and not transcript:
        raise HTTPException(status_code=422, detail="prompts require transcript")
    if diarize and not transcript:
        raise HTTPException(status_code=422, detail="diarize requires transcript")
    task_id = uuid.uuid4()
    artifact = task_dir(artifacts_root, user.username, task_id)
    artifact.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "language": language,
        "audio_only": audio_only,
        "transcript": transcript,
        "diarize": diarize,
        "prompts": norm,
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
    async def list_tasks_for_user_filtered(
        self,
        user_id: uuid.UUID,
        *,
        status: Any = None,
        limit: int = 20,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> list[Any]: ...


async def list_tasks(
    *,
    user: _UserLike,
    repo: _RepoListLike,
    status: Literal["queued", "running", "completed", "failed", "paused", "canceled", "archived"] | None = None,
    limit: int = 20,
    sort: Literal["created_at", "updated_at", "title"] = "updated_at",
    order: Literal["asc", "desc"] = "desc",
) -> list[TaskSummary]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    tasks = await repo.list_tasks_for_user_filtered(
        uuid.UUID(user.id),
        status=status,
        limit=limit,
        sort=sort,
        order=order,
    )
    return [
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

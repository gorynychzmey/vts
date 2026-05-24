from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from vts.mcp.schemas import ProgressCounts, SubmitVideoResult, SummaryResult, TaskStatusResult, TaskSummary, TranscriptResult, WaitResult
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
) -> SubmitVideoResult:
    """Create a new task in the queued state and notify the worker."""
    if not url or not url.strip():
        raise HTTPException(status_code=422, detail="url is required")
    task_id = uuid.uuid4()
    artifact = task_dir(artifacts_root, user.username, task_id)
    artifact.mkdir(parents=True, exist_ok=True)
    task = await repo.create_task(
        user_id=uuid.UUID(user.id),
        source_url=url.strip(),
        options={},
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
    if stage in _SUMMARY_STAGES:
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


async def get_summary(
    *,
    task_id: uuid.UUID,
    user: _UserLike,
    repo: _RepoStatusLike,
) -> SummaryResult:
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.summary_path:
        raise HTTPException(status_code=404, detail="Summary is not ready")
    path = Path(task.summary_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Summary file missing")
    return SummaryResult(task_id=task.id, content=path.read_text(encoding="utf-8"), format="markdown")


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


_TERMINAL = {"completed", "failed", "canceled"}
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

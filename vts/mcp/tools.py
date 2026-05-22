from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import HTTPException

from vts.api.main import _summary_progress_for_task
from vts.mcp.schemas import ProgressCounts, SubmitVideoResult, TaskStatusResult, TaskSummary, TranscriptResult
from vts.services.storage import task_dir


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
    asr_current, asr_total = asr_map.get(task.id, (0, 0))
    summary_current, summary_total = _summary_progress_for_task(task)
    return TaskStatusResult(
        task_id=task.id,
        status=str(task.status),
        stage=_stage_label(task),
        asr_progress=ProgressCounts(current=asr_current, total=asr_total),
        summary_progress=ProgressCounts(current=summary_current, total=summary_total),
        error=task.error_message,
        updated_at=task.updated_at,
    )


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

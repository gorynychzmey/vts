from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException

from vts.mcp.schemas import SubmitVideoResult
from vts.services.storage import task_dir


async def submit_video(
    *,
    url: str,
    user,
    repo,
    bus,
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
        data={"status": task.status if isinstance(task.status, str) else task.status.value},
    )
    return SubmitVideoResult(task_id=task.id, status=task.status, created_at=task.created_at)

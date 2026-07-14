from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.metrics import MetricsEmitter
from vts.pipeline.processor import TaskPaused, _TaskGone
from vts.pipeline.token_budget import TokenBudgetConfig
from vts.services.llm_backends import discover_n_ctx
from vts.services.push import notify_user as push_notify_user


class PipelineContext:
    """Shared services + infra helpers handed to every pipeline Step.

    Constructed from a TaskProcessor; exposes the same backend services and a
    verbatim copy of the processor's infra-helper methods (with `self.`
    attribute references rewritten to this context's attributes). The canonical
    copies live here; TaskProcessor keeps its `_*` originals during migration.
    """

    def __init__(self, proc) -> None:
        self.session_factory = proc.session_factory
        self.redis = proc.redis
        self.settings = proc.settings
        self.bus = proc.bus
        self.lanes = proc.lanes
        self.whisper = proc.whisper
        self.llm = proc._llm
        self._task_metrics = proc._task_metrics
        self._task_n_ctx = proc._task_n_ctx

    async def check_paused(self, task_id: uuid.UUID) -> None:
        """Raise TaskPaused if a pause has been requested for this task."""
        if await self.bus.is_pause_requested(task_id):
            raise TaskPaused()

    async def refresh_task(self, session: AsyncSession, task: Any) -> None:
        """Refresh the task row, translating a mid-flight delete into _TaskGone.

        The API delete endpoint removes the row in its own session; a concurrent
        refresh here then raises InvalidRequestError ("Could not refresh
        instance"). That is not a pipeline failure — the user discarded the
        task — so surface it as _TaskGone for a quiet exit (vts-d64)."""
        try:
            await session.refresh(task)
        except InvalidRequestError as exc:
            raise _TaskGone() from exc

    async def mark_waiting(self, task_id: uuid.UUID, user_id: str, queue: str) -> None:
        """Flip running→waiting when a gpu slot is contended (race-guarded)."""
        async with self.session_factory() as session:
            repo = Repo(session)
            changed = await repo.transition_task_status(
                task_id, [TaskStatus.running], TaskStatus.waiting
            )
            await session.commit()
        if changed:
            await self.bus.publish_event(
                user_id=user_id, task_id=str(task_id),
                event="task_status", data={"status": TaskStatus.waiting.value, "queue": queue},
            )

    async def mark_running(self, task_id: uuid.UUID, user_id: str) -> None:
        """Flip waiting→running when a gpu slot is granted (race-guarded)."""
        async with self.session_factory() as session:
            repo = Repo(session)
            changed = await repo.transition_task_status(
                task_id, [TaskStatus.waiting], TaskStatus.running
            )
            await session.commit()
        if changed:
            await self.bus.publish_event(
                user_id=user_id, task_id=str(task_id),
                event="task_status", data={"status": TaskStatus.running.value},
            )

    def gpu_slot(self, task_id: uuid.UUID, user_id: str, cls: str):
        """Acquire the gpu lane for one GPU call, flipping waiting/running as needed."""
        return self.lanes.slot(
            "gpu", task_id, cls,
            on_wait=lambda: self.mark_waiting(task_id, user_id, "gpu"),
            on_grant=lambda: self.mark_running(task_id, user_id),
        )

    def get_emitter(self, task_id: uuid.UUID) -> MetricsEmitter | None:
        """Return the active MetricsEmitter for a task, or None if absent."""
        return getattr(self, "_task_metrics", {}).get(str(task_id))

    async def get_n_ctx(self, task_id: uuid.UUID, logger: logging.Logger) -> int:
        """Discover the model's context window once per task run, caching it.

        Backend detection (LiteLLM / Ollama / llama-server) lives in
        vts.services.llm_backends; when nothing matches, the configured
        summary_n_ctx constant applies."""
        if not hasattr(self, "_task_n_ctx"):
            self._task_n_ctx = {}
        key = str(task_id)
        if key in self._task_n_ctx:
            return self._task_n_ctx[key]
        fallback = int(getattr(self.settings, "summary_n_ctx", TokenBudgetConfig().n_ctx))
        backend, n_ctx = await discover_n_ctx(
            url=self.settings.llm_url,
            api_key=getattr(self.settings, "llm_api_key", None),
            model=self.settings.llm_model,
            fallback_n_ctx=fallback,
        )
        if backend == "generic":
            logger.warning(
                "token budget: no LLM backend detected, using fallback n_ctx=%d", n_ctx
            )
        else:
            logger.info("token budget: n_ctx=%d (backend=%s)", n_ctx, backend)
        self._task_n_ctx[key] = n_ctx
        return n_ctx

    async def send_push_safe(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        # Push notifications must never block or break the pipeline.
        try:
            await push_notify_user(session, self.settings, user_id, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("push: notify_user failed: %s", exc)

    async def task_url(self, task_id: uuid.UUID) -> str:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("Task not found")
            return task.source_url

    async def get_user_preferred_ytdlp_client(self, user_id: uuid.UUID) -> str | None:
        async with self.session_factory() as session:
            repo = Repo(session)
            return await repo.get_user_preferred_ytdlp_client(user_id)

    async def set_user_preferred_ytdlp_client(self, user_id: uuid.UUID, player_client: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            await repo.set_user_preferred_ytdlp_client(user_id, player_client)
            await session.commit()

    async def persist_summary_progress(self, task_id: uuid.UUID, current: int, total: int) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            await repo.set_task_summary_progress(task, current, total)
            await session.commit()

    async def save_task_source_title(self, task_id: uuid.UUID, title: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            if task.source_title:
                # The user already named the task (e.g. renamed it while it
                # was queued) — the discovered media title must not clobber it.
                return
            task.source_title = title
            await session.commit()

    async def persist_detected_language(self, task_id: uuid.UUID, language: str, confidence: float) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            options = dict(task.options or {})
            options["detected_language"] = language
            options["detected_language_confidence"] = float(confidence)
            task.options = options
            await session.commit()

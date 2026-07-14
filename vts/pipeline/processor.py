from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
import uuid
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.db.models import StepStatus, TaskStatus
from vts.db.repo import Repo
from vts.pipeline.steps.base import StepState
from vts.pipeline.steps.registry import resolve_step
from vts.pipeline.types import build_dag_steps, lane_for_step
from vts.worker.lanes import LaneManager
from vts.services.llm_backends import discover_n_ctx
from vts.services.task_progress import selected_prompt_refs
from vts.services.push import notify_user as push_notify_user
from vts.services.redis_bus import RedisBus
from vts.services.storage import cow_copy_dir, ensure_task_dirs
from vts.pipeline.token_budget import TokenBudgetConfig
from vts.services.summarizer import LLMClient
from vts.services.transcription import WhisperBackend, create_whisper_backend
from vts.metrics import MetricsEmitter, aggregate_task_metrics


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskPaused(Exception):
    """Raised when the processor detects a pause request mid-step."""


class _TaskGone(Exception):
    """The task row vanished mid-flight (deleted/canceled by the API).

    Distinct from a pipeline failure: the user already discarded the task, so
    the processor must exit quietly without publishing a `failed` event/push.
    """


class TaskProcessor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        lanes: LaneManager | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.bus = RedisBus(redis, settings)
        self.lanes = lanes or LaneManager(settings)
        self.whisper: WhisperBackend = create_whisper_backend(settings.whisper_url, settings.whisper_backend)
        self._task_metrics: dict[str, MetricsEmitter] = {}
        self._task_n_ctx: dict[str, int] = {}
        self._llm = LLMClient(url=settings.llm_url, api_key=settings.llm_api_key)
        from vts.pipeline.context import PipelineContext
        self._ctx = PipelineContext(self)

    async def _check_paused(self, task_id: uuid.UUID) -> None:
        """Raise TaskPaused if a pause has been requested for this task."""
        if await self.bus.is_pause_requested(task_id):
            raise TaskPaused()

    async def _refresh_task(self, session: AsyncSession, task: Any) -> None:
        """Refresh the task row, translating a mid-flight delete into _TaskGone.

        The API delete endpoint removes the row in its own session; a concurrent
        refresh here then raises InvalidRequestError ("Could not refresh
        instance"). That is not a pipeline failure — the user discarded the
        task — so surface it as _TaskGone for a quiet exit (vts-d64)."""
        try:
            await session.refresh(task)
        except InvalidRequestError as exc:
            raise _TaskGone() from exc

    async def _mark_waiting(self, task_id: uuid.UUID, user_id: str, queue: str) -> None:
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

    async def _mark_running(self, task_id: uuid.UUID, user_id: str) -> None:
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

    def _gpu_slot(self, task_id: uuid.UUID, user_id: str, cls: str):
        """Acquire the gpu lane for one GPU call, flipping waiting/running as needed."""
        return self.lanes.slot(
            "gpu", task_id, cls,
            on_wait=lambda: self._mark_waiting(task_id, user_id, "gpu"),
            on_grant=lambda: self._mark_running(task_id, user_id),
        )

    def _get_emitter(self, task_id: uuid.UUID) -> MetricsEmitter | None:
        """Return the active MetricsEmitter for a task, or None if absent."""
        return getattr(self, "_task_metrics", {}).get(str(task_id))

    async def _get_n_ctx(self, task_id: uuid.UUID, logger: logging.Logger) -> int:
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

    async def process_task(self, task_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            if task.status in {TaskStatus.canceled, TaskStatus.completed, TaskStatus.archived}:
                return
            task_options = self._task_options(task.options)

            # --- donor clone check ---
            _clone_logger = logging.getLogger(f"vts.clone.{task.id}")
            donor = None
            if self.settings.features_donor_clone:
                try:
                    donor = await repo.find_completed_donor(
                        source_url=task.source_url,
                        options=task.options,
                        exclude_user_id=task.user_id,
                    )
                except Exception as _exc:
                    _clone_logger.warning("donor lookup failed, falling back to normal pipeline: %s", _exc)
            if donor is not None:
                try:
                    await self._clone_from_donor(session, repo, task, donor)
                    await session.commit()
                    await self.bus.publish_event(
                        user_id=str(task.user_id),
                        task_id=str(task.id),
                        event="task_status",
                        data={"status": TaskStatus.completed.value},
                    )
                    return
                except Exception as _exc:
                    _clone_logger.warning(
                        "donor clone failed (donor=%s), falling back to normal pipeline: %s",
                        donor.id,
                        _exc,
                    )
                    await session.rollback()
                    # Reload task after rollback
                    task = await repo.get_task_by_id(task_id)
                    if task is None:
                        return
            # --- end donor clone check ---

            await repo.set_task_status(task, TaskStatus.running)
            await session.commit()
            await self.bus.publish_event(
                user_id=str(task.user_id),
                task_id=str(task.id),
                event="task_status",
                data={"status": task.status.value},
            )

            task_root = Path(task.artifact_dir)
            dirs = ensure_task_dirs(task_root)
            logger = self._task_logger(task_id=task.id, log_path=dirs["logs"] / "task.log")

            run_id = str(uuid.uuid4())
            jsonl_path = (
                self.settings.metrics_jsonl_path
                if self.settings.metrics_enabled
                else None
            )
            emitter = MetricsEmitter(
                task_id=str(task.id),
                run_id=run_id,
                jsonl_path=jsonl_path,
                enabled=self.settings.metrics_enabled,
            )
            self._task_metrics[str(task.id)] = emitter
            _task_wall_t0 = time.monotonic()

            try:
                for step_name in build_dag_steps(task_options):
                    await self._check_paused(task.id)
                    await self._refresh_task(session, task)
                    if task.status == TaskStatus.canceled:
                        return
                    await self._run_step(
                        session,
                        repo,
                        task.id,
                        str(task.user_id),
                        step_name,
                        dirs,
                        logger,
                        task_options,
                    )
                    await self._refresh_task(session, task)
                    await asyncio.sleep(self.settings.services_database_write_throttle_ms / 1000.0)
                await self._cleanup_media(dirs["media"])
                await repo.set_task_status(task, TaskStatus.completed)
                await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": task.status.value},
                )
                await self._send_push_safe(
                    session,
                    task.user_id,
                    {
                        "task_id": str(task.id),
                        "status": TaskStatus.completed.value,
                        "title": task.source_title or task.source_url,
                    },
                )
                _task_wall_ms = round((time.monotonic() - _task_wall_t0) * 1000)
                emitter.emit({
                    "stage": "task.final",
                    "status": "ok",
                    "t_wall_ms": _task_wall_ms,
                    "aggregates": aggregate_task_metrics(emitter.all_events()),
                })
            except TaskPaused:
                logger.info("task paused: %s", task.id)
                await self.bus.clear_pause_request(task.id)
                await self._refresh_task(session, task)
                if task.status != TaskStatus.paused:
                    await repo.set_task_status(task, TaskStatus.paused)
                    await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": "paused"},
                )
            except _TaskGone:
                # Row deleted/canceled mid-flight by the API. The user already
                # discarded the task, so exit quietly — no failed event/push
                # (vts-d64). The session's aborted transaction is rolled back by
                # the `async with self.session_factory()` context on exit.
                logger.info("task %s deleted mid-flight; exiting quietly", task_id)
                await self.bus.clear_pause_request(task_id)
            except Exception as exc:
                logger.exception("pipeline failed: %s", exc)
                raw_error = str(exc)
                failure_code = classify_failure_code(raw_error)
                await repo.set_task_status(task, TaskStatus.failed, error_message=raw_error)
                await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": TaskStatus.failed.value, "error": raw_error, "failure_code": failure_code},
                )
                await self._send_push_safe(
                    session,
                    task.user_id,
                    {
                        "task_id": str(task.id),
                        "status": TaskStatus.failed.value,
                        "title": task.source_title or task.source_url,
                        "error": raw_error,
                        "failure_code": failure_code,
                    },
                )
                _task_wall_ms = round((time.monotonic() - _task_wall_t0) * 1000)
                emitter.emit({
                    "stage": "task.final",
                    "status": "error",
                    "t_wall_ms": _task_wall_ms,
                })
            finally:
                # Use the local task_id, not task.id: after a mid-flight delete
                # the ORM object is expired and attribute access would trigger a
                # lazy DB load off the event loop (MissingGreenlet) — vts-d64.
                self._task_metrics.pop(str(task_id), None)
                self._task_n_ctx.pop(str(task_id), None)

    async def _run_step(
        self,
        session: AsyncSession,
        repo: Repo,
        task_id: uuid.UUID,
        user_id: str,
        step_name: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
    ) -> None:
        step = await repo.upsert_step(task_id, step_name)
        if not self._is_step_enabled(step_name, task_options):
            if step.status != StepStatus.skipped:
                await repo.set_step_status(step, StepStatus.skipped, message="Disabled by task options")
                await session.commit()
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="step",
                data={"name": step_name, "status": StepStatus.skipped.value},
            )
            return

        # Registry-aware dispatch. Migrated steps (Tasks 3-5) resolve to a Step
        # object and run via `step_obj.run`; everything not yet migrated raises
        # KeyError from resolve_step and falls through to the legacy method path
        # below. The registry is still empty in this task, so every step takes
        # the legacy branch — behavior is identical.
        st = StepState(
            task_id=task_id,
            user_id=user_id,
            dirs=dirs,
            logger=logger,
            task_options=task_options,
        )
        try:
            step_obj = resolve_step(step_name)
        except KeyError:
            step_obj = None

        if step_obj is not None:
            if step.status == StepStatus.completed and await step_obj.already_done(
                self._ctx, st
            ):
                return
            lane = step_obj.lane
        else:
            # Legacy path for not-yet-migrated steps (removed in Task 6). Finalize
            # steps (summarize_final + finalize:*) are now resolved by the registry
            # to FinalizePromptStep, so they never reach this branch.
            method = getattr(self, f"step_{step_name}")
            if step.status == StepStatus.completed and await method(
                task_id,
                user_id,
                dirs,
                logger,
                task_options,
                dry_run=True,
            ):
                return
            lane = lane_for_step(step_name)

        # Download and ffmpeg steps run under a shared concurrency lane so the
        # same phase of different tasks does not saturate the network / CPU. The
        # step stays `pending` while queued (task-level `waiting` covers the UI);
        # it only turns `running` once the lane slot is granted.
        if lane is not None:
            lane_cm = self.lanes.slot(
                lane,
                task_id,
                on_wait=lambda: self._mark_waiting(task_id, user_id, lane),
                on_grant=lambda: self._mark_running(task_id, user_id),
            )
        else:
            lane_cm = contextlib.nullcontext()

        async with lane_cm:
            await repo.set_step_status(step, StepStatus.running)
            await session.commit()
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="step",
                data={"name": step_name, "status": StepStatus.running.value},
            )
            _step_t0 = time.monotonic()
            try:
                if step_obj is not None:
                    await step_obj.run(self._ctx, st)
                else:
                    await method(task_id, user_id, dirs, logger, task_options, dry_run=False)
                _step_wall_ms = round((time.monotonic() - _step_t0) * 1000)
                await repo.set_step_status(step, StepStatus.completed)
                await session.commit()
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="step",
                    data={"name": step_name, "status": StepStatus.completed.value},
                )
                _em = self._get_emitter(task_id)
                if _em:
                    _em.emit({"stage": step_name, "status": "ok", "t_wall_ms": _step_wall_ms})
            except Exception as exc:
                _step_wall_ms = round((time.monotonic() - _step_t0) * 1000)
                await repo.set_step_status(step, StepStatus.failed, message=str(exc))
                await session.commit()
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="step",
                    data={"name": step_name, "status": StepStatus.failed.value, "error": str(exc)},
                )
                _em = self._get_emitter(task_id)
                if _em:
                    _em.emit({"stage": step_name, "status": "error", "t_wall_ms": _step_wall_ms})
                raise

    async def _send_push_safe(
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

    async def _cleanup_media(self, media_dir: Path) -> None:
        cutoff = utcnow() - timedelta(hours=self.settings.media_ttl_hours)
        for file in media_dir.glob("*"):
            if not file.is_file():
                continue
            modified = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if self.settings.media_ttl_hours <= 0 or modified <= cutoff:
                file.unlink(missing_ok=True)

    async def _task_url(self, task_id: uuid.UUID) -> str:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("Task not found")
            return task.source_url

    async def _get_user_preferred_ytdlp_client(self, user_id: uuid.UUID) -> str | None:
        async with self.session_factory() as session:
            repo = Repo(session)
            return await repo.get_user_preferred_ytdlp_client(user_id)

    async def _set_user_preferred_ytdlp_client(self, user_id: uuid.UUID, player_client: str) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            await repo.set_user_preferred_ytdlp_client(user_id, player_client)
            await session.commit()

    async def _persist_summary_progress(self, task_id: uuid.UUID, current: int, total: int) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            await repo.set_task_summary_progress(task, current, total)
            await session.commit()

    async def _save_task_source_title(self, task_id: uuid.UUID, title: str) -> None:
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

    async def _persist_detected_language(self, task_id: uuid.UUID, language: str, confidence: float) -> None:
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

    def _task_options(self, raw_options: dict[str, Any] | None) -> dict[str, Any]:
        return dict(raw_options or {})

    async def _clone_from_donor(
        self,
        session: AsyncSession,
        repo: Repo,
        task: Any,
        donor: Any,
    ) -> None:
        """Clone a completed donor task into the current task (CoW file copy + DB metadata)."""
        logger = logging.getLogger(f"vts.clone.{task.id}")

        donor_dir = Path(donor.artifact_dir)
        task_dir = Path(task.artifact_dir)

        logger.info(
            "cloning from donor task %s (user %s) -> task %s (user %s)",
            donor.id,
            donor.user_id,
            task.id,
            task.user_id,
        )

        # CoW-copy all artifacts from donor dir to task dir
        if donor_dir.exists():
            await asyncio.to_thread(cow_copy_dir, donor_dir, task_dir)

        # Fix up paths that are stored as absolute strings pointing to donor dir
        def _remap(path_str: str | None) -> str | None:
            if path_str is None:
                return None
            try:
                rel = Path(path_str).relative_to(donor_dir)
                return str(task_dir / rel)
            except ValueError:
                return path_str

        # Keep a user-assigned name (rename while queued); only fill a blank one.
        task.source_title = task.source_title or donor.source_title
        task.transcript_path = _remap(donor.transcript_path)
        task.summary_path = _remap(donor.summary_path)

        # Copy steps (mark all as skipped / cloned)
        from vts.db.models import Step, StepStatus

        for donor_step in donor.steps:
            step = Step(
                task_id=task.id,
                name=donor_step.name,
                status=StepStatus.skipped,
                attempt=0,
                message="cloned from donor",
            )
            session.add(step)

        # Copy ASR segments
        await repo.clone_asr_segments(donor.id, task.id)

        # Mark task as completed
        task.status = TaskStatus.completed
        task.error_message = None
        from vts.db.models import utcnow as _utcnow
        task.updated_at = _utcnow()
        await session.flush()

    def _task_flag(self, options: dict[str, Any], key: str, *, default: bool) -> bool:
        value = options.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _is_step_enabled(self, step_name: str, task_options: dict[str, Any]) -> bool:
        transcript_enabled = self._task_flag(task_options, "transcript", default=True)
        if not transcript_enabled:
            return step_name == "download"
        # Finalize steps (summarize_final + finalize:*) only ever appear in the DAG
        # because build_dag_steps emitted them for a selected prompt, so they are
        # always enabled here.
        if step_name == "summarize_final" or step_name.startswith("finalize:"):
            return True
        # The summary head (map-reduce that prepares the shared `merged` input) runs
        # whenever ANY prompt is selected, and is skipped only when none are. The
        # selection is the source of truth — not the legacy `summary` flag.
        if step_name in {
            "prepare_llama_model",
            "prepare_summary_chunks",
            "summarize_windows",
            "pack_window_notes",
        }:
            return bool(selected_prompt_refs(task_options))
        return True

    def _task_logger(self, task_id: uuid.UUID, log_path: Path) -> logging.Logger:
        logger = logging.getLogger(f"task.{task_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = True
        if not any(isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_path) for handler in logger.handlers):
            handler = logging.FileHandler(log_path, encoding="utf-8")
            fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            if self.settings.timezone:
                tz = zoneinfo.ZoneInfo(self.settings.timezone)
                fmt.converter = lambda secs: datetime.fromtimestamp(secs, tz=tz).timetuple()
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        return logger

    async def delete_task_artifacts(self, artifact_dir: str) -> None:
        await asyncio.to_thread(shutil.rmtree, artifact_dir, True)

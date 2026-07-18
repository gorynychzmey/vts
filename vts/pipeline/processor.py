from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
import uuid
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.db.models import StepStatus, TaskStatus
from vts.db.repo import Repo
from vts.services import task_status as _ts
from vts.pipeline.steps.base import StepState
from vts.pipeline.steps.registry import resolve_step
from vts.pipeline.steps.diarization import DiarizationCancelled
from vts.pipeline.types import build_dag_steps
from vts.worker.lanes import LaneManager
from vts.services.task_progress import selected_prompt_refs
from vts.services.redis_bus import RedisBus
from vts.services.storage import cow_copy_dir, ensure_task_dirs
from vts.services.summarizer import LLMClient
from vts.services.transcription import WhisperBackend, create_whisper_backend
from vts.services.diarization import DiarizationBackend, create_diarization_backend
from vts.metrics import MetricsEmitter, aggregate_task_metrics


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskPaused(Exception):
    """Raised when the processor detects a pause request mid-step."""


class TaskAwaitingInput(Exception):
    """Raised by a step that needs a human decision before the pipeline can continue.

    Unlike TaskPaused (a pause request from outside), this is the step itself
    declaring it cannot proceed without input — e.g. MatchSpeakersStep when a
    detected speaker doesn't auto-resolve against the registry.
    """

    def __init__(self, step: str) -> None:
        self.step = step
        super().__init__(step)


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
        self.diarization: DiarizationBackend = create_diarization_backend(
            settings.diarization_url, settings.diarization_backend
        )
        self._task_metrics: dict[str, MetricsEmitter] = {}
        self._task_n_ctx: dict[str, int] = {}
        self._llm = LLMClient(url=settings.llm_url, api_key=settings.llm_api_key)
        from vts.pipeline.context import PipelineContext
        self._ctx = PipelineContext(self)

    async def process_task(self, task_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            if _ts.is_skippable_on_start(task.status):
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
                    await self._ctx.check_paused(task.id)
                    await self._ctx.refresh_task(session, task)
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
                    await self._ctx.refresh_task(session, task)
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
                await self._ctx.send_push_safe(
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
                await self._ctx.refresh_task(session, task)
                if task.status != TaskStatus.paused:
                    await repo.set_task_status(task, TaskStatus.paused)
                    await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": "paused"},
                )
            except TaskAwaitingInput as e:
                logger.info("task awaiting input: %s (step=%s)", task.id, e.step)
                await self._ctx.refresh_task(session, task)
                if task.status != TaskStatus.awaiting_input:
                    await repo.set_awaiting_input(task, e.step)
                    await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": "awaiting_input", "awaiting_step": e.step},
                )
            except _TaskGone:
                # Row deleted/canceled mid-flight by the API. The user already
                # discarded the task, so exit quietly — no failed event/push
                # (vts-d64). The session's aborted transaction is rolled back by
                # the `async with self.session_factory()` context on exit.
                logger.info("task %s deleted mid-flight; exiting quietly", task_id)
                await self.bus.clear_pause_request(task_id)
            except DiarizationCancelled:
                # Same as _TaskGone, but noticed from inside the step: only
                # diarization runs long enough to be worth interrupting rather
                # than waiting out. The user discarded it, so exit quietly.
                logger.info("task %s cancelled during diarization; exiting quietly", task_id)
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
                await self._ctx.send_push_safe(
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

        # Registry dispatch. Every step resolves to a Step object (media /
        # transcription / summarization registries + FinalizePromptStep). A
        # KeyError from resolve_step now signals a real bug (an unknown step name
        # reached the DAG) and is intentionally left to propagate.
        st = StepState(
            task_id=task_id,
            user_id=user_id,
            dirs=dirs,
            logger=logger,
            task_options=task_options,
        )
        step_obj = resolve_step(step_name)

        if step.status == StepStatus.completed and await step_obj.already_done(
            self._ctx, st
        ):
            return
        lane = step_obj.lane

        # Download and ffmpeg steps run under a shared concurrency lane so the
        # same phase of different tasks does not saturate the network / CPU. The
        # step stays `pending` while queued (task-level `waiting` covers the UI);
        # it only turns `running` once the lane slot is granted.
        if lane is not None:
            lane_cm = self.lanes.slot(
                lane,
                task_id,
                on_wait=lambda: self._ctx.mark_waiting(task_id, user_id, lane),
                on_grant=lambda: self._ctx.mark_running(task_id, user_id),
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
                await step_obj.run(self._ctx, st)
                _step_wall_ms = round((time.monotonic() - _step_t0) * 1000)
                await repo.set_step_status(step, StepStatus.completed)
                await session.commit()
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="step",
                    data={"name": step_name, "status": StepStatus.completed.value},
                )
                _em = self._ctx.get_emitter(task_id)
                if _em:
                    _em.emit({"stage": step_name, "status": "ok", "t_wall_ms": _step_wall_ms})
            except TaskAwaitingInput:
                # Asymmetry fix (vts-80i): unlike TaskPaused (raised BETWEEN
                # steps, in check_paused), TaskAwaitingInput is raised FROM
                # INSIDE a step's run() — the step itself declaring it did its
                # job and now needs a human decision. It must NOT be treated
                # like a step failure: the step (e.g. MatchSpeakersStep)
                # already wrote its output (speaker_matches.json) and made a
                # valid pause decision, so mark it completed, exactly like the
                # success path above. This is what lets `already_done` (gated
                # on step.status == completed) short-circuit the step on
                # resume — otherwise a resumed task with any speaker left
                # intentionally anonymous would re-run match_speakers,
                # re-decide to pause, and loop into awaiting_input forever.
                _step_wall_ms = round((time.monotonic() - _step_t0) * 1000)
                await repo.set_step_status(step, StepStatus.completed)
                await session.commit()
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="step",
                    data={"name": step_name, "status": StepStatus.completed.value},
                )
                _em = self._ctx.get_emitter(task_id)
                if _em:
                    _em.emit({"stage": step_name, "status": "ok", "t_wall_ms": _step_wall_ms})
                raise
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
                _em = self._ctx.get_emitter(task_id)
                if _em:
                    _em.emit({"stage": step_name, "status": "error", "t_wall_ms": _step_wall_ms})
                raise

    async def _cleanup_media(self, media_dir: Path) -> None:
        cutoff = utcnow() - timedelta(hours=self.settings.media_ttl_hours)
        for file in media_dir.glob("*"):
            if not file.is_file():
                continue
            modified = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if self.settings.media_ttl_hours <= 0 or modified <= cutoff:
                file.unlink(missing_ok=True)

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

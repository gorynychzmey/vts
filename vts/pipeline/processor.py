from __future__ import annotations

import asyncio
import contextlib
import functools
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
from vts.services.prompt_registry import list_system_prompts, parse_ref
from vts.services.prompt_results import upsert_result_entry
from vts.services.task_progress import selected_prompt_refs
from vts.services.push import notify_user as push_notify_user
from vts.services.redis_bus import RedisBus
from vts.services.storage import cow_copy_dir, ensure_task_dirs, write_json
from vts.pipeline.token_budget import (
    TokenBudgetConfig,
    SummarizationMetrics,
    clamp,
    compute_final_budget,
    compute_pack_budget,
    compute_segment_budget,
    derive_window_tokens,
    fits_in_context,
    fits_whole_transcript,
    is_context_overflow_error,
    uncap_segment_for_input,
    whole_transcript_possible,
)
from vts.services.summarizer import (
    LLMClient,
    inject_budget_vars,
    load_prompt,
    parse_json_response,
)
from vts.services.transcription import WhisperBackend, create_whisper_backend
from vts.metrics import MetricsEmitter, QualityAnalyzer, aggregate_task_metrics


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskPaused(Exception):
    """Raised when the processor detects a pause request mid-step."""


class _TaskGone(Exception):
    """The task row vanished mid-flight (deleted/canceled by the API).

    Distinct from a pipeline failure: the user already discarded the task, so
    the processor must exit quietly without publishing a `failed` event/push.
    """


class _WholeTranscriptOverflow(Exception):
    """Whole-transcript rewrite hit a context overflow; carries the cause."""


_SEGMENT_PROMPT_FALLBACK = (
    "Rewrite the transcript segment as clean fluent text: remove fillers,"
    " interjections, false starts and repetitions, but keep all content,"
    " wording and order. Do not summarize."
)


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

    def _token_budget_config(self, n_ctx: int) -> TokenBudgetConfig:
        _defaults = TokenBudgetConfig()
        s = self.settings

        def _get(name: str, default: object) -> object:
            return getattr(s, f"summary_{name}", default)

        return TokenBudgetConfig(
            n_ctx=n_ctx,
            safety_margin=int(_get("safety_margin", _defaults.safety_margin)),
            segment_ratio=float(_get("segment_ratio", _defaults.segment_ratio)),
            segment_min_ratio=float(_get("segment_min_ratio", _defaults.segment_min_ratio)),
            segment_max_ratio=float(_get("segment_max_ratio", _defaults.segment_max_ratio)),
            segment_min_floor=int(_get("segment_min_floor", _defaults.segment_min_floor)),
            segment_max_cap=int(_get("segment_max_cap", _defaults.segment_max_cap)),
            pack_ratio=float(_get("pack_ratio", _defaults.pack_ratio)),
            pack_min_ratio=float(_get("pack_min_ratio", _defaults.pack_min_ratio)),
            pack_max_ratio=float(_get("pack_max_ratio", _defaults.pack_max_ratio)),
            pack_min_floor=int(_get("pack_min_floor", _defaults.pack_min_floor)),
            pack_batch_max_input_tokens=int(_get("pack_batch_max_input_tokens", _defaults.pack_batch_max_input_tokens)),
            final_ratio=float(_get("final_ratio", _defaults.final_ratio)),
            final_min_ratio=float(_get("final_min_ratio", _defaults.final_min_ratio)),
            final_max_ratio=float(_get("final_max_ratio", _defaults.final_max_ratio)),
        )

    @property
    def _tokenizer_path(self) -> str | None:
        p = self.settings.llm_tokenizer_path
        return str(p) if p is not None else None

    def _log_metrics(self, logger: logging.Logger, metrics: SummarizationMetrics) -> None:
        logger.info(
            "token_budget stage=%s input=%d target=%d actual=%d packing=%s pass_count=%d",
            metrics.stage_name,
            metrics.input_tokens,
            metrics.target_tokens,
            metrics.actual_output_tokens,
            metrics.packing_triggered,
            metrics.packing_pass_count,
        )

    def _render_prompt_budget_vars(
        self,
        prompt: str,
        *,
        language: str | None = None,
        input_tokens: int | None = None,
        target_tokens: int | None = None,
        target_ratio: float | None = None,
    ) -> str:
        if language is not None:
            prompt = self._render_prompt_with_language(prompt, language)
        prompt = inject_budget_vars(
            prompt,
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            target_ratio=target_ratio,
        )
        return prompt

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
            # Legacy path for not-yet-migrated steps (removed in Task 6).
            # Finalize steps are generated per selected prompt and dispatched by
            # a dynamic name that cannot map to a real method (colons are illegal
            # in identifiers, and the handler needs extra source/id args). Bind a
            # functools.partial so the fixed call sites below stay unchanged.
            if step_name == "summarize_final":
                method = functools.partial(
                    self.step_finalize_prompt, source="system", id="summary"
                )
            elif step_name.startswith("finalize:"):
                f_source, f_id = parse_ref(step_name.split(":", 1)[1])
                method = functools.partial(
                    self.step_finalize_prompt, source=f_source, id=f_id
                )
            else:
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

    async def step_prepare_llama_model(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        marker = dirs["outputs"] / "llama_model_ready.json"
        target_model = self.settings.llm_model
        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and str(payload.get("model", "")) == target_model:
                return True
        if dry_run:
            return False

        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="llama_model_progress",
            data={"status": "loading", "model": target_model},
        )
        logger.info("warming llama model: %s", target_model)
        try:
            logger.info("waiting for gpu slot: llama warmup")
            async with self._gpu_slot(task_id, user_id, "llm"):
                logger.info("gpu slot acquired: llama warmup")
                raw = await self._llm.chat_completion(
                    model=target_model,
                    system_prompt='Return compact JSON: {"status":"ready"}.',
                    user_prompt="Warm up model for upcoming summarization.",
                    timeout_seconds=1200,
                    max_tokens=32,
                    temperature=self.settings.llm_temperature,
                    top_p=self.settings.llm_top_p,
                    min_p=self.settings.llm_min_p,
                    repeat_penalty=self.settings.llm_repeat_penalty,
                    thinking=self.settings.llm_thinking,
                )
            self._log_payload(logger, "llama warmup response", raw)
        except Exception as exc:
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="llama_model_progress",
                data={"status": "failed", "model": target_model, "error": str(exc)},
            )
            raise

        parsed = parse_json_response(raw)
        write_json(
            marker,
            {
                "model": target_model,
                "ready_at": utcnow().isoformat(),
                "response": parsed,
            },
        )
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="llama_model_progress",
            data={"status": "ready", "model": target_model},
        )
        logger.info("llama model is ready: %s", target_model)
        return True

    async def step_prepare_summary_chunks(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        summary_dir = dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        chunks_file = summary_dir / "chunks.json"
        if chunks_file.exists():
            return True
        if dry_run:
            return False

        transcript_json = dirs["outputs"] / "transcript.json"
        if not transcript_json.exists():
            raise RuntimeError("Missing transcript for summarization")
        transcript = json.loads(transcript_json.read_text(encoding="utf-8")).get("text", "")
        if not isinstance(transcript, str) or not transcript.strip():
            logger.info("summary chunks skipped: empty transcript")
            write_json(chunks_file, {"chunks": [], "segmentation": "split"})
            write_json(dirs["outputs"] / "summary_chunks.json", {"chunks": [], "segmentation": "split"})
            return True

        logger.info("summary chunk preparation started")
        mode = str(getattr(self.settings, "summary_segmentation", "auto") or "auto")
        budget_cfg = self._token_budget_config(await self._get_n_ctx(task_id, logger))
        timeout_seconds = int(getattr(self.settings, "llm_chat_timeout_seconds", 600))
        segment_prompt = self._render_prompt_with_language(
            load_prompt(self.settings.prompts_dir, "segment_prompt.md", _SEGMENT_PROMPT_FALLBACK),
            self._effective_language(task_options, dirs),
        )
        prompt_tokens = await self._llm.count_tokens(
            text=segment_prompt,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )
        transcript_tokens = await self._llm.count_tokens(
            text=transcript,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )

        send_whole = False
        if mode == "never":
            if not whole_transcript_possible(budget_cfg, prompt_tokens, transcript_tokens):
                raise RuntimeError(
                    f"summary segmentation=never: transcript (~{transcript_tokens} tokens)"
                    f" cannot fit the model context window (n_ctx={budget_cfg.n_ctx})"
                    " in one piece"
                )
            send_whole = True
        elif mode == "auto":
            send_whole = fits_whole_transcript(budget_cfg, prompt_tokens, transcript_tokens)

        if send_whole:
            chunks = [transcript]
            logger.info(
                "summary segmentation: whole transcript (mode=%s tokens=%d prompt=%d n_ctx=%d)",
                mode, transcript_tokens, prompt_tokens, budget_cfg.n_ctx,
            )
        else:
            window_tokens = derive_window_tokens(
                budget_cfg,
                prompt_tokens,
                cap=int(getattr(self.settings, "summary_segment_window_cap", 8192)),
            )
            logger.info(
                "summary segmentation: split (mode=%s tokens=%d window=%d n_ctx=%d)",
                mode, transcript_tokens, window_tokens, budget_cfg.n_ctx,
            )
            chunks = await self._llm.chunk_text(
                text=transcript,
                model=self.settings.llm_model,
                window_tokens=window_tokens,
                overlap_ratio=0.15,
                tokenizer_path=self._tokenizer_path,
            )
        payload = {"chunks": chunks, "segmentation": "whole" if send_whole else "split"}
        logger.info("summary chunk preparation finished: %s windows", len(chunks))
        write_json(chunks_file, payload)
        write_json(dirs["outputs"] / "summary_chunks.json", payload)
        return True

    async def step_summarize_windows(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        summary_dir = dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        output = summary_dir / "windows.json"
        output_mirror = dirs["outputs"] / "window_summaries.json"
        if dry_run:
            if not output.exists():
                return False
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False
            windows = payload.get("windows") if isinstance(payload, dict) else None
            return isinstance(windows, list)

        output_language = self._effective_language(task_options, dirs)
        segment_prompt = self._render_prompt_with_language(
            load_prompt(self.settings.prompts_dir, "segment_prompt.md", _SEGMENT_PROMPT_FALLBACK),
            output_language,
        )
        chunks_file = summary_dir / "chunks.json"
        if not chunks_file.exists():
            chunks_file = dirs["outputs"] / "summary_chunks.json"
        if not chunks_file.exists():
            raise RuntimeError("Missing summary chunks")
        chunks_payload = json.loads(chunks_file.read_text(encoding="utf-8"))
        chunks = chunks_payload.get("chunks") if isinstance(chunks_payload, dict) else None
        if not isinstance(chunks, list):
            raise RuntimeError("Invalid summary chunks payload")
        whole_mode = chunks_payload.get("segmentation") == "whole"
        total_windows = len(chunks)

        windows_by_index: dict[int, dict[str, Any]] = {}
        if output.exists():
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            raw_windows = payload.get("windows") if isinstance(payload, dict) else None
            if isinstance(raw_windows, list):
                for item in raw_windows:
                    if not isinstance(item, dict):
                        continue
                    raw_index = item.get("window_index")
                    try:
                        idx = int(raw_index)
                    except (TypeError, ValueError):
                        continue
                    if idx < 1:
                        continue
                    summary_payload = item.get("summary")
                    path = item.get("path")
                    if not isinstance(path, str) or not path.strip():
                        path = str(summary_dir / f"window_{idx:02d}.txt")
                    windows_by_index[idx] = {
                        "window_index": idx,
                        "summary": summary_payload,
                        "path": path,
                    }

        file_pattern = re.compile(r"^window_(\d+)\.txt$")
        for window_path in sorted(summary_dir.glob("window_*.txt")):
            match = file_pattern.match(window_path.name)
            if not match:
                continue
            idx = int(match.group(1))
            if idx in windows_by_index:
                continue
            content = window_path.read_text(encoding="utf-8")
            try:
                parsed = json.loads(content)
                summary: str | dict = parsed if isinstance(parsed, dict) else content
            except json.JSONDecodeError:
                summary = content
            windows_by_index[idx] = {
                "window_index": idx,
                "summary": summary,
                "path": str(window_path),
            }

        for idx in list(windows_by_index.keys()):
            if idx > total_windows:
                windows_by_index.pop(idx, None)

        restored = sum(1 for idx in windows_by_index if 1 <= idx <= total_windows)
        if restored:
            logger.info("restored summarized windows: %s/%s", restored, total_windows)
        if restored == total_windows:
            ordered = [windows_by_index[idx] for idx in sorted(windows_by_index)]
            write_json(output, {"windows": ordered})
            write_json(output_mirror, {"windows": ordered})
            redacted_path = dirs["outputs"] / "redacted_transcript.txt"
            redacted_path.write_text(
                "".join(str(w.get("summary", "")).rstrip("\n") + "\n\n" for w in ordered),
                encoding="utf-8",
            )
            logger.info("window summaries already complete: %s", total_windows)
            return True

        logger.info("window summarization started: %s windows", len(chunks))
        budget_cfg = self._token_budget_config(await self._get_n_ctx(task_id, logger))
        total_parts = len(chunks) + 1
        # A whole-transcript rewrite generates output comparable to the input
        # size — that is final-stage territory, not a 2k-window call.
        timeout_seconds = int(
            getattr(self.settings, "llm_final_timeout_seconds", 1800)
            if whole_mode
            else getattr(self.settings, "llm_chat_timeout_seconds", 600)
        )
        redacted_path = dirs["outputs"] / "redacted_transcript.txt"
        redacted_path.write_text(
            "".join(
                str(windows_by_index[i].get("summary", "")).rstrip("\n") + "\n\n"
                for i in sorted(windows_by_index)
            ),
            encoding="utf-8",
        )
        while True:
            try:
                for idx, chunk in enumerate(chunks, start=1):
                    await self._check_paused(task_id)
                    if idx in windows_by_index:
                        logger.info("window %s/%s already summarized, skipping", idx, len(chunks))
                        await self.bus.publish_event(
                            user_id=user_id,
                            task_id=str(task_id),
                            event="summary_progress",
                            data={"current": idx, "total": total_parts},
                            throttle_key="summary_progress",
                        )
                        await self._persist_summary_progress(task_id, idx, total_parts)
                        continue
                    logger.info("summarizing window %s/%s", idx, len(chunks))

                    # Stage A: adaptive token budget
                    user_prompt = f"Window {idx}/{len(chunks)}\n\n{chunk}"
                    input_tokens = await self._llm.count_tokens(
                        text=user_prompt,
                        model=self.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=self._tokenizer_path,
                    )
                    window_cfg = uncap_segment_for_input(budget_cfg, input_tokens)
                    target_tokens, min_out, max_out = compute_segment_budget(input_tokens, window_cfg)
                    budgeted_prompt = self._render_prompt_budget_vars(
                        segment_prompt,
                        input_tokens=input_tokens,
                        target_tokens=target_tokens,
                        target_ratio=window_cfg.segment_ratio,
                    )
                    logger.info(
                        "window %s/%s token_budget input=%d target=%d min=%d max=%d",
                        idx, len(chunks), input_tokens, target_tokens, min_out, max_out,
                    )
                    logger.info("waiting for gpu slot: summarize window %s/%s", idx, len(chunks))
                    _win_t_q0 = time.monotonic()
                    async with self._gpu_slot(task_id, user_id, "llm"):
                        _win_t_q_ms = round((time.monotonic() - _win_t_q0) * 1000)
                        logger.info("gpu slot acquired: summarize window %s/%s", idx, len(chunks))
                        _win_t0 = time.monotonic()
                        try:
                            raw = await self._llm.chat_completion(
                                model=self.settings.llm_model,
                                system_prompt=budgeted_prompt,
                                user_prompt=user_prompt,
                                timeout_seconds=timeout_seconds,
                                temperature=self.settings.llm_temperature,
                                top_p=self.settings.llm_top_p,
                                min_p=self.settings.llm_min_p,
                                repeat_penalty=self.settings.llm_repeat_penalty,
                                cache_prompt=True,
                                use_json_format=False,
                                thinking=self.settings.llm_thinking,
                                num_ctx=budget_cfg.n_ctx,
                            )
                        except RuntimeError as exc:
                            if whole_mode and is_context_overflow_error(str(exc)):
                                mode = str(getattr(self.settings, "summary_segmentation", "auto") or "auto")
                                if mode == "never":
                                    raise RuntimeError(
                                        "summary segmentation=never: the model cannot process"
                                        f" the transcript in one piece (n_ctx={budget_cfg.n_ctx}): {exc}"
                                    ) from exc
                                raise _WholeTranscriptOverflow() from exc
                            raise
                        _win_t_ms = round((time.monotonic() - _win_t0) * 1000)
                    actual_output_tokens = await self._llm.count_tokens(
                        text=raw,
                        model=self.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=self._tokenizer_path,
                    )
                    self._log_metrics(logger, SummarizationMetrics(
                        stage_name="segment",
                        input_tokens=input_tokens,
                        target_tokens=target_tokens,
                        actual_output_tokens=actual_output_tokens,
                    ))
                    self._log_payload(logger, f"llm window response index={idx}", raw, max_chars=200)
                    _win_em = self._get_emitter(task_id)
                    if _win_em:
                        _n_ctx = budget_cfg.n_ctx
                        _win_em.emit({
                            "stage": "summarize.segment",
                            "status": "ok",
                            "segment_id": idx,
                            "t_wall_ms": _win_t_ms,
                            "t_queue_ms": _win_t_q_ms,
                            "llm_prompt_tokens": input_tokens,
                            "llm_completion_tokens": actual_output_tokens,
                            "llm_total_tokens": input_tokens + actual_output_tokens,
                            "llm_tok_per_s": round(actual_output_tokens / (_win_t_ms / 1000), 2) if _win_t_ms > 0 else None,
                            "llm_ctx_utilization": round(input_tokens / _n_ctx, 4) if _n_ctx > 0 else None,
                            "retries": 0,
                            **QualityAnalyzer(
                                shingle_n=self.settings.metrics_redundancy_shingle_n,
                                simhash_bits=self.settings.metrics_redundancy_simhash_bits,
                                max_hamming=self.settings.metrics_redundancy_max_hamming,
                            ).analyze(
                                summary_text=raw,
                                transcript_text=chunk,
                                prompt_tokens=input_tokens,
                                completion_tokens=actual_output_tokens,
                            ),
                        })
                    window_path = summary_dir / f"window_{idx:02d}.txt"
                    window_path.write_text(raw, encoding="utf-8")
                    windows_by_index[idx] = {"window_index": idx, "summary": raw, "path": str(window_path)}
                    ordered = [windows_by_index[item_idx] for item_idx in sorted(windows_by_index)]
                    write_json(output, {"windows": ordered})
                    write_json(output_mirror, {"windows": ordered})
                    redacted_path = dirs["outputs"] / "redacted_transcript.txt"
                    with redacted_path.open("a", encoding="utf-8") as rf:
                        rf.write(raw.rstrip("\n") + "\n\n")
                    await self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event="segment_summary_text",
                        data={"index": idx, "total": total_windows, "text": raw},
                    )
                    await self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event="summary_progress",
                        data={"current": idx, "total": total_parts},
                        throttle_key="summary_progress",
                    )
                    await self._persist_summary_progress(task_id, idx, total_parts)
            except _WholeTranscriptOverflow as overflow:
                logger.warning(
                    "whole-transcript rewrite exceeded the context window; "
                    "falling back to segmentation: %s",
                    overflow.__cause__,
                )
                prompt_tokens = await self._llm.count_tokens(
                    text=segment_prompt,
                    model=self.settings.llm_model,
                    timeout_seconds=int(getattr(self.settings, "llm_chat_timeout_seconds", 600)),
                    tokenizer_path=self._tokenizer_path,
                )
                window_tokens = derive_window_tokens(
                    budget_cfg,
                    prompt_tokens,
                    cap=int(getattr(self.settings, "summary_segment_window_cap", 8192)),
                )
                chunks = await self._llm.chunk_text(
                    text=chunks[0],
                    model=self.settings.llm_model,
                    window_tokens=window_tokens,
                    overlap_ratio=0.15,
                    tokenizer_path=self._tokenizer_path,
                )
                split_payload = {"chunks": chunks, "segmentation": "split"}
                write_json(summary_dir / "chunks.json", split_payload)
                write_json(dirs["outputs"] / "summary_chunks.json", split_payload)
                whole_mode = False
                windows_by_index = {}
                total_windows = len(chunks)
                total_parts = len(chunks) + 1
                timeout_seconds = int(getattr(self.settings, "llm_chat_timeout_seconds", 600))
                redacted_path.write_text("", encoding="utf-8")
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="summary_progress",
                    data={"current": 0, "total": total_parts},
                    throttle_key="summary_progress",
                )
                await self._persist_summary_progress(task_id, 0, total_parts)
                logger.info("fallback segmentation: %s windows", len(chunks))
                continue
            break
        ordered = [windows_by_index[idx] for idx in sorted(windows_by_index)]
        write_json(output, {"windows": ordered})
        write_json(output_mirror, {"windows": ordered})
        logger.info("window summaries generated: %s", len(ordered))
        return True

    async def step_pack_window_notes(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        """Stage B — pack/dedup window notes so they fit in the final context budget.

        If prompt + notes + estimated output + safety margin already fit within n_ctx,
        the step is a no-op (writes the passthrough marker and exits).  Otherwise it
        compresses notes in batches until they fit.
        """
        summary_dir = dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        packed_file = summary_dir / "packed_notes.json"
        if packed_file.exists():
            return True
        if dry_run:
            return False

        # Load window notes
        windows_file = summary_dir / "windows.json"
        if not windows_file.exists():
            windows_file = dirs["outputs"] / "window_summaries.json"
        if not windows_file.exists():
            raise RuntimeError("Missing window summaries for packing step")
        windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
        if not isinstance(windows, list):
            raise RuntimeError("Invalid window summaries payload")

        output_language = self._effective_language(task_options, dirs)
        timeout_seconds = int(getattr(self.settings, "llm_final_timeout_seconds", 1800))
        budget_cfg = self._token_budget_config(await self._get_n_ctx(task_id, logger))

        # Load final prompt to measure its token cost
        final_prompt_text = self._render_prompt_budget_vars(
            self._render_prompt_with_language(
                load_prompt(
                    self.settings.prompts_dir,
                    "global_prompt.md",
                    "Produce a structured knowledge document from the notes.",
                ),
                output_language,
            ),
        )
        final_prompt_tokens = await self._llm.count_tokens(
            text=final_prompt_text,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )
        logger.info(
            "pack_window_notes: final_prompt_tokens=%d",
            final_prompt_tokens,
        )

        # Count total tokens of all notes
        notes_texts: list[str] = [self._extract_window_text(w) for w in windows]
        note_token_counts: list[int] = []
        for text in notes_texts:
            tc = await self._llm.count_tokens(
                text=text,
                model=self.settings.llm_model,
                timeout_seconds=timeout_seconds,
                tokenizer_path=self._tokenizer_path,
            )
            note_token_counts.append(tc)
        total_notes_tokens = sum(note_token_counts)

        packing_triggered = not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens)
        logger.info(
            "pack_window_notes: total_notes_tokens=%d packing_needed=%s",
            total_notes_tokens,
            packing_triggered,
        )

        packing_pass_count = 0

        if packing_triggered:
            pack_prompt_template = self._render_prompt_with_language(
                load_prompt(
                    self.settings.prompts_dir,
                    "pack_prompt.md",
                    "Integrate and deduplicate the following notes. "
                    "Target output: ~${TARGET_WORDS} words (~${TARGET_RATIO}% of input, input: ~${INPUT_WORDS} words).\n"
                    "Output language: ${LANG}.",
                ),
                output_language,
            )

            current_texts = notes_texts
            current_token_counts = note_token_counts

            while not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens) and len(current_texts) > 0:
                packing_pass_count += 1
                logger.info(
                    "packing pass %d: total_tokens=%d notes=%d",
                    packing_pass_count,
                    total_notes_tokens,
                    len(current_texts),
                )

                # Split notes into batches not exceeding pack_batch_max_input_tokens
                batches: list[list[str]] = []
                current_batch: list[str] = []
                current_batch_tokens = 0
                for note_text, note_tc in zip(current_texts, current_token_counts):
                    if (
                        current_batch
                        and current_batch_tokens + note_tc > budget_cfg.pack_batch_max_input_tokens
                    ):
                        batches.append(current_batch)
                        current_batch = []
                        current_batch_tokens = 0
                    current_batch.append(note_text)
                    current_batch_tokens += note_tc
                if current_batch:
                    batches.append(current_batch)

                new_texts: list[str] = []
                new_token_counts: list[int] = []
                for b_idx, batch in enumerate(batches, 1):
                    await self._check_paused(task_id)
                    batch_input = "\n\n".join(batch)
                    batch_input_tokens = await self._llm.count_tokens(
                        text=batch_input,
                        model=self.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=self._tokenizer_path,
                    )
                    target_tokens, min_out, max_out = compute_pack_budget(
                        batch_input_tokens, budget_cfg
                    )
                    pack_system_prompt = self._render_prompt_budget_vars(
                        pack_prompt_template,
                        input_tokens=batch_input_tokens,
                        target_tokens=target_tokens,
                        target_ratio=budget_cfg.pack_ratio,
                    )
                    logger.info(
                        "pack batch %d/%d: input=%d target=%d min=%d max=%d",
                        b_idx, len(batches), batch_input_tokens, target_tokens, min_out, max_out,
                    )
                    async with self._gpu_slot(task_id, user_id, "llm"):
                        packed_text = await self._llm.chat_completion(
                            model=self.settings.llm_model,
                            system_prompt=pack_system_prompt,
                            user_prompt=batch_input,
                            timeout_seconds=timeout_seconds,
                            temperature=self.settings.llm_temperature,
                            top_p=self.settings.llm_top_p,
                            min_p=self.settings.llm_min_p,
                            repeat_penalty=self.settings.llm_repeat_penalty,
                            cache_prompt=True,
                            use_json_format=False,
                            thinking=self.settings.llm_thinking,
                            num_ctx=budget_cfg.n_ctx,
                        )
                    packed_tc = await self._llm.count_tokens(
                        text=packed_text,
                        model=self.settings.llm_model,
                        timeout_seconds=timeout_seconds,
                        tokenizer_path=self._tokenizer_path,
                    )
                    self._log_metrics(logger, SummarizationMetrics(
                        stage_name="pack",
                        input_tokens=batch_input_tokens,
                        target_tokens=target_tokens,
                        actual_output_tokens=packed_tc,
                        packing_triggered=True,
                        packing_pass_count=packing_pass_count,
                    ))
                    new_texts.append(packed_text)
                    new_token_counts.append(packed_tc)

                current_texts = new_texts
                current_token_counts = new_token_counts
                total_notes_tokens = sum(current_token_counts)

                # Guard: stop if packing produced a single note and still doesn't fit
                if len(current_texts) == 1 and not fits_in_context(budget_cfg, final_prompt_tokens, total_notes_tokens):
                    logger.warning(
                        "packing converged to a single note but still exceeds budget "
                        "(%d tokens); proceeding anyway",
                        total_notes_tokens,
                    )
                    break

            notes_texts = current_texts

        write_json(
            packed_file,
            {
                "notes": notes_texts,
                "packing_triggered": packing_triggered,
                "packing_pass_count": packing_pass_count,
                "total_notes_tokens": total_notes_tokens,
            },
        )
        logger.info(
            "pack_window_notes complete: notes=%d total_tokens=%d packing_triggered=%s passes=%d",
            len(notes_texts),
            total_notes_tokens,
            packing_triggered,
            packing_pass_count,
        )
        return True

    async def resolve_prompt_text(
        self, source: str, id: str, output_language: str | None, user_id: str
    ) -> str:
        """Resolve the system-prompt text for a finalize run.

        For ``system`` prompts, load the registered prompt file (rendered with the
        output language) — for ``system/summary`` this reproduces today's
        ``global_prompt.md`` rendering exactly. For ``user`` prompts, load the
        ``system_prompt`` column from the DB.
        """
        if source == "system":
            sysdef = next((p for p in list_system_prompts() if p.key == id), None)
            if sysdef is None:
                raise RuntimeError(f"unknown system prompt: {id}")
            return self._render_prompt_with_language(
                load_prompt(
                    self.settings.prompts_dir,
                    sysdef.file,
                    "Produce a structured knowledge document from the notes.\n\nOutput language: ${LANG}.",
                ),
                output_language,
            )
        async with self.session_factory() as session:
            repo = Repo(session)
            row = await repo.get_prompt(uuid.UUID(user_id), uuid.UUID(id))
        if row is None:
            raise RuntimeError(f"user prompt not found: {id}")
        return self._render_prompt_with_language(row.system_prompt, output_language)

    async def _prompt_display_name(self, source: str, id: str, user_id: str) -> str:
        """Display name stored in the prompt_results index.

        For system prompts: the i18n name key (UI localises it). For user prompts:
        the Prompt row's name.
        """
        if source == "system":
            sysdef = next((p for p in list_system_prompts() if p.key == id), None)
            return sysdef.i18n_name_key if sysdef else id
        async with self.session_factory() as session:
            repo = Repo(session)
            row = await repo.get_prompt(uuid.UUID(user_id), uuid.UUID(id))
        return row.name if row is not None else id

    async def _persist_prompt_result(
        self, task_id: uuid.UUID, source: str, id: str, name: str, path: str
    ) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            options = dict(task.options or {})
            entries = upsert_result_entry(
                options, source, id, name, path, status="completed"
            )
            await repo.set_task_prompt_results(task, entries)
            await session.commit()

    async def step_finalize_prompt(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
        *,
        source: str,
        id: str,
    ) -> bool:
        # Defense-in-depth: validate the id BEFORE it is used to build any result
        # path. A user-source id must be a UUID; this rejects path-traversal ids
        # (e.g. "../../etc/passwd") regardless of downstream call ordering.
        if source == "user":
            try:
                uuid.UUID(id)
            except (ValueError, TypeError):
                raise RuntimeError(f"invalid user prompt id: {id!r}")
        is_summary = source == "system" and id == "summary"
        summary_dir = dirs["root"] / "summary"
        summary_dir.mkdir(parents=True, exist_ok=True)
        if is_summary:
            summary_json = summary_dir / "final.json"
            summary_md = summary_dir / "final.md"
        else:
            results_dir = summary_dir / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            summary_json = results_dir / f"{source}__{id}.json"
            summary_md = results_dir / f"{source}__{id}.md"
        if summary_json.exists() and summary_md.exists():
            if is_summary:
                async with self.session_factory() as session:
                    repo = Repo(session)
                    task = await repo.get_task_by_id(task_id)
                    if task is None:
                        raise RuntimeError("task not found during final summary restore")
                    summary_path = str(summary_md)
                    if task.summary_path != summary_path:
                        task.summary_path = summary_path
                        await session.commit()
            else:
                name = await self._prompt_display_name(source, id, user_id)
                await self._persist_prompt_result(task_id, source, id, name, str(summary_md))
            return True
        if dry_run:
            return False

        output_language = self._effective_language(task_options, dirs)
        timeout_seconds = int(getattr(self.settings, "llm_final_timeout_seconds", 1800))
        budget_cfg = self._token_budget_config(await self._get_n_ctx(task_id, logger))

        # Load packed notes if the packing step ran, else fall back to window summaries.
        # fallback_windows: list passed to _summarize_hierarchical if flat call fails.
        # merged: the user_prompt for the flat final call.
        packed_file = summary_dir / "packed_notes.json"
        if packed_file.exists():
            packed_payload = json.loads(packed_file.read_text(encoding="utf-8"))
            packed_notes: list[str] = packed_payload.get("notes", [])
            if not isinstance(packed_notes, list):
                packed_notes = []
            packing_triggered: bool = bool(packed_payload.get("packing_triggered", False))
            packing_pass_count: int = int(packed_payload.get("packing_pass_count", 0))
            merged = "\n\n".join(packed_notes)
            total_windows = len(packed_notes)
            total_parts = total_windows + 1
            logger.info(
                "final summary: using packed notes (%d) packing_triggered=%s",
                len(packed_notes),
                packing_triggered,
            )
        else:
            windows_file = summary_dir / "windows.json"
            if not windows_file.exists():
                windows_file = dirs["outputs"] / "window_summaries.json"
            if not windows_file.exists():
                raise RuntimeError("Missing window summaries")
            windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
            if not isinstance(windows, list):
                raise RuntimeError("Invalid window summaries payload")
            # Build merged with [Segment N] prefix (same as original behaviour)
            parts: list[str] = []
            for w in windows:
                idx = w.get("window_index", "?")
                text = self._extract_window_text(w)
                parts.append(f"[Segment {idx}]\n{text}" if text else f"[Segment {idx}]")
            merged = "\n\n".join(parts)
            packing_triggered = False
            packing_pass_count = 0
            total_windows = len(windows)
            total_parts = total_windows + 1

        logger.info("final summary generation started: notes=%s", total_windows)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="summary_progress",
            data={"current": total_windows, "total": total_parts},
        )
        await self._persist_summary_progress(task_id, total_windows, total_parts)

        global_prompt_base = await self.resolve_prompt_text(
            source, id, output_language, user_id
        )
        # Stage C: adaptive token budget
        input_tokens = await self._llm.count_tokens(
            text=merged,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )
        target_tokens, min_out, max_out = compute_final_budget(input_tokens, budget_cfg)
        final_prompt_tokens = await self._llm.count_tokens(
            text=global_prompt_base,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )
        global_prompt = self._render_prompt_budget_vars(
            global_prompt_base,
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            target_ratio=budget_cfg.final_ratio,
        )
        logger.info(
            "final summary token_budget input=%d target=%d min=%d max=%d",
            input_tokens, target_tokens, min_out, max_out,
        )
        logger.info(
            "waiting for gpu slot: final summary (notes=%s payload_bytes=%s)",
            total_windows,
            len(merged.encode("utf-8")),
        )
        _fin_t_q0 = time.monotonic()
        async with self._gpu_slot(task_id, user_id, "llm"):
            _fin_t_q_ms = round((time.monotonic() - _fin_t_q0) * 1000)
            logger.info("gpu slot acquired: final summary")
            _fin_t0 = time.monotonic()
            raw = await self._llm.chat_completion(
                model=self.settings.llm_model,
                system_prompt=global_prompt,
                user_prompt=merged,
                timeout_seconds=timeout_seconds,
                temperature=self.settings.llm_temperature,
                top_p=self.settings.llm_top_p,
                min_p=self.settings.llm_min_p,
                repeat_penalty=self.settings.llm_repeat_penalty,
                use_json_format=False,
                thinking=self.settings.llm_thinking,
                num_ctx=budget_cfg.n_ctx,
            )
            _fin_t_ms = round((time.monotonic() - _fin_t0) * 1000)

        actual_output_tokens = await self._llm.count_tokens(
            text=raw,
            model=self.settings.llm_model,
            timeout_seconds=timeout_seconds,
            tokenizer_path=self._tokenizer_path,
        )
        self._log_metrics(logger, SummarizationMetrics(
            stage_name="final",
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            actual_output_tokens=actual_output_tokens,
            packing_triggered=packing_triggered,
            packing_pass_count=packing_pass_count,
        ))
        self._log_payload(logger, "llm final summary response", raw, max_chars=200)
        _fin_em = self._get_emitter(task_id)
        if _fin_em:
            _n_ctx = budget_cfg.n_ctx
            # Load transcript text for mismatch comparison
            _transcript_text = ""
            _transcript_json = dirs["outputs"] / "transcript.json"
            if _transcript_json.exists():
                try:
                    _transcript_text = json.loads(_transcript_json.read_text(encoding="utf-8")).get("text", "")
                except Exception:
                    pass
            _fin_em.emit({
                "stage": "summarize.global",
                "status": "ok",
                "t_wall_ms": _fin_t_ms,
                "t_queue_ms": _fin_t_q_ms,
                "llm_prompt_tokens": input_tokens,
                "llm_completion_tokens": actual_output_tokens,
                "llm_total_tokens": input_tokens + actual_output_tokens,
                "llm_tok_per_s": round(actual_output_tokens / (_fin_t_ms / 1000), 2) if _fin_t_ms > 0 else None,
                "llm_ctx_utilization": round(input_tokens / _n_ctx, 4) if _n_ctx > 0 else None,
                "packing_triggered": packing_triggered,
                "packing_pass_count": packing_pass_count,
                "retries": 0,
                **QualityAnalyzer(
                    shingle_n=self.settings.metrics_redundancy_shingle_n,
                    simhash_bits=self.settings.metrics_redundancy_simhash_bits,
                    max_hamming=self.settings.metrics_redundancy_max_hamming,
                ).analyze(
                    summary_text=raw,
                    transcript_text=_transcript_text or merged,
                    prompt_tokens=input_tokens,
                    completion_tokens=actual_output_tokens,
                ),
            })
        write_json(summary_json, {"raw": raw})
        summary_md.write_text(raw, encoding="utf-8")
        if is_summary:
            # Back-compat: the canonical summary mirrors into outputs/summary.*.
            write_json(dirs["outputs"] / "summary.json", {"raw": raw})
            (dirs["outputs"] / "summary.md").write_text(raw, encoding="utf-8")
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="summary_progress",
            data={"current": total_parts, "total": total_parts},
        )
        await self._persist_summary_progress(task_id, total_parts, total_parts)
        logger.info("final summary generated")

        if is_summary:
            async with self.session_factory() as session:
                repo = Repo(session)
                task = await repo.get_task_by_id(task_id)
                if task is None:
                    raise RuntimeError("task not found during final summary")
                task.summary_path = str(summary_md)
                await session.commit()
        name = await self._prompt_display_name(source, id, user_id)
        await self._persist_prompt_result(task_id, source, id, name, str(summary_md))
        return True

    def _extract_window_text(self, window: dict[str, Any]) -> str:
        summary = window.get("summary", {})
        if isinstance(summary, str):
            return summary.strip()
        if not isinstance(summary, dict):
            return str(summary).strip()
        # Legacy JSON dict summaries — check for raw/summary keys first
        for key in ("summary", "raw"):
            val = summary.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # Legacy structured JSON summary — render as readable text
        parts: list[str] = []
        for key, val in summary.items():
            if key == "raw":
                continue
            if isinstance(val, list):
                parts.append(f"{key}: " + "; ".join(str(i) for i in val))
            elif isinstance(val, str) and val.strip():
                parts.append(f"{key}: {val.strip()}")
        return "\n".join(parts)

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

    def _normalize_language(self, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None

    def _effective_language(self, task_options: dict[str, Any], dirs: dict[str, Path]) -> str | None:
        explicit = self._normalize_language(task_options.get("language"))
        if explicit:
            return explicit
        detected = self._normalize_language(task_options.get("detected_language"))
        if detected:
            return detected
        marker = dirs["outputs"] / "language_detection.json"
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return self._normalize_language(payload.get("language"))

    def _render_prompt_with_language(self, prompt: str, language: str | None) -> str:
        value = self._language_display_name(language)
        return prompt.replace("${LANG}", value)

    def _language_display_name(self, language: str | None) -> str:
        lang = (language or "en").strip().lower()
        mapping = {
            "en": "English",
            "ru": "Russian",
            "de": "German",
            "fr": "French",
            "es": "Spanish",
        }
        return mapping.get(lang, lang)

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

    def _log_payload(self, logger: logging.Logger, prefix: str, payload: Any, max_chars: int = 4000) -> None:
        try:
            raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=True)
        except Exception:
            raw = str(payload)
        truncated = raw if len(raw) <= max_chars else raw[:max_chars] + "...<truncated>"
        logger.info("%s: %s", prefix, truncated)

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

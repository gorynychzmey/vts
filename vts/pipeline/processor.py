from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import Settings
from vts.core.failures import classify_failure_code
from vts.db.models import StepStatus, TaskStatus
from vts.db.repo import Repo
from vts.pipeline.types import DAG_STEPS
from vts.services.downloader import download_video_and_audio
from vts.services.heavy_slot import HeavySlot
from vts.services.media import (
    build_segments,
    detect_silence_points,
    export_segments,
    extract_audio_16k_mono,
    probe_duration,
    trim_initial_silence,
)
from vts.services.redis_bus import RedisBus
from vts.services.storage import ensure_task_dirs, write_json
from vts.pipeline.token_budget import (
    TokenBudgetConfig,
    SummarizationMetrics,
    clamp,
    compute_final_budget,
    compute_final_in_budget,
    compute_pack_budget,
    compute_segment_budget,
)
from vts.services.summarizer import (
    chunk_text,
    count_tokens,
    inject_budget_vars,
    llama_chat_completion,
    load_prompt,
    parse_json_response,
)
from vts.services.transcription import normalize_whisper_output, transcribe_with_whisper
from vts.metrics import MetricsEmitter, QualityAnalyzer, aggregate_task_metrics


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskProcessor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.bus = RedisBus(redis, settings)
        self.heavy_slot = HeavySlot(redis, settings)
        self._task_metrics: dict[str, MetricsEmitter] = {}

    def _get_emitter(self, task_id: uuid.UUID) -> MetricsEmitter | None:
        """Return the active MetricsEmitter for a task, or None if absent."""
        return getattr(self, "_task_metrics", {}).get(str(task_id))

    def _token_budget_config(self) -> TokenBudgetConfig:
        _defaults = TokenBudgetConfig()
        s = self.settings

        def _get(name: str, default: object) -> object:
            return getattr(s, f"summary_{name}", default)

        return TokenBudgetConfig(
            n_ctx=int(_get("n_ctx", _defaults.n_ctx)),
            safety_margin=int(_get("safety_margin", _defaults.safety_margin)),
            final_out_budget=int(_get("final_out_budget", _defaults.final_out_budget)),
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
        final_in_budget: int | None = None,
        final_out_budget: int | None = None,
    ) -> str:
        if language is not None:
            prompt = self._render_prompt_with_language(prompt, language)
        prompt = inject_budget_vars(
            prompt,
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            final_in_budget=final_in_budget,
            final_out_budget=final_out_budget,
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
                for step_name in DAG_STEPS:
                    await session.refresh(task)
                    if task.status == TaskStatus.paused:
                        await self.bus.publish_event(
                            user_id=str(task.user_id),
                            task_id=str(task.id),
                            event="task_status",
                            data={"status": "paused"},
                        )
                        return
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
                    await session.refresh(task)
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
                _task_wall_ms = round((time.monotonic() - _task_wall_t0) * 1000)
                emitter.emit({
                    "stage": "task.final",
                    "status": "ok",
                    "t_wall_ms": _task_wall_ms,
                    "aggregates": aggregate_task_metrics(emitter.all_events()),
                })
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
                _task_wall_ms = round((time.monotonic() - _task_wall_t0) * 1000)
                emitter.emit({
                    "stage": "task.final",
                    "status": "error",
                    "t_wall_ms": _task_wall_ms,
                })
            finally:
                self._task_metrics.pop(str(task.id), None)

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

    async def step_download(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        audio_only = self._task_flag(task_options, "audio_only", default=False)
        video_file = dirs["media"] / "video.mkv"
        audio_file = next(dirs["media"].glob("audio.original.*"), None)
        if audio_only and audio_file:
            return True
        if not audio_only and video_file.exists() and audio_file:
            return True
        if dry_run:
            # Media files may have been cleaned up after a completed run.
            # If audio segments already exist, download is not needed again.
            return any(dirs["segments"].glob("*.wav"))


        source_url = await self._task_url(task_id)
        user_uuid = uuid.UUID(user_id)
        preferred_youtube_client = await self._get_user_preferred_ytdlp_client(user_uuid)
        if preferred_youtube_client:
            logger.info("using saved yt-dlp youtube client for user: %s", preferred_youtube_client)
        loop = asyncio.get_running_loop()
        captured_title: list[str] = []

        def sync_progress(phase: str, payload: dict[str, Any]) -> None:
            if not captured_title:
                title = payload.get("media_title")
                if title and isinstance(title, str):
                    captured_title.append(title.strip())
            merged_data = {"phase": phase, **payload}
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event="media_progress",
                        data=merged_data,
                        throttle_key="media_progress",
                    )
                )
            )

        def sync_phase(phase: str, status: str) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event="phase",
                        data={"phase": phase, "status": status},
                    )
                )
            )

        _, _, selected_youtube_client = await asyncio.to_thread(
            download_video_and_audio,
            source_url=source_url,
            media_dir=dirs["media"],
            progress_cb=sync_progress,
            phase_cb=sync_phase,
            logger=logger,
            audio_only=audio_only,
            preferred_youtube_client=preferred_youtube_client,
            ytdlp_cookies_file=self.settings.ytdlp_cookies_file,
            ytdlp_cookies_from_browser=self.settings.ytdlp_cookies_from_browser,
            ytdlp_youtube_player_client=self.settings.ytdlp_youtube_player_client,
            ytdlp_youtube_po_token=self.settings.ytdlp_youtube_po_token,
            ytdlp_verbose=self.settings.ytdlp_verbose,
        )
        if selected_youtube_client and selected_youtube_client != preferred_youtube_client:
            await self._set_user_preferred_ytdlp_client(user_uuid, selected_youtube_client)
            logger.info("saved yt-dlp youtube client for user: %s", selected_youtube_client)
        if captured_title:
            await self._save_task_source_title(task_id, captured_title[0])
        logger.info("download finished")
        return True

    async def step_extract_audio(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        output = dirs["media"] / "audio_16k.wav"
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        # After trim step we remove audio_16k.wav, so resume from later stages
        # must treat the trimmed WAV as a valid completion marker too.
        if trimmed.exists():
            return True
        if output.exists():
            return True
        if dry_run:
            # Media files may have been cleaned up after a completed run.
            return any(dirs["segments"].glob("*.wav"))
        audio_file = next(dirs["media"].glob("audio.original.*"), None)
        if not audio_file:
            raise RuntimeError("Missing downloaded audio file")
        await asyncio.to_thread(
            extract_audio_16k_mono,
            audio_file,
            output,
            dirs["logs"] / "task.log",
        )
        logger.info("audio extraction finished")
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "extract_audio", "status": "done"},
        )
        return True

    async def step_trim_initial_silence(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        source = dirs["media"] / "audio_16k.wav"
        output = dirs["media"] / "audio_16k_trimmed.wav"
        marker = dirs["outputs"] / "audio_preprocess.json"
        if output.exists() and marker.exists():
            return True
        if dry_run:
            return False
        if not source.exists():
            raise RuntimeError("Missing extracted WAV")

        trimmed_seconds = await asyncio.to_thread(
            trim_initial_silence,
            source,
            output,
            dirs["logs"] / "task.log",
            threshold_db=self.settings.trim_silence_threshold_db,
            min_duration_sec=self.settings.trim_silence_min_duration_sec,
            max_trim_seconds=self.settings.trim_silence_max_seconds,
        )
        payload = {
            "source": str(source),
            "output": str(output),
            "trimmed_seconds": round(trimmed_seconds, 3),
            "threshold_db": self.settings.trim_silence_threshold_db,
            "min_duration_sec": self.settings.trim_silence_min_duration_sec,
            "max_trim_seconds": self.settings.trim_silence_max_seconds,
        }
        write_json(marker, payload)
        source.unlink(missing_ok=True)
        logger.info("initial silence trim finished: trimmed=%.3fs", trimmed_seconds)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "trim_initial_silence", "status": "done", "trimmed_seconds": round(trimmed_seconds, 3)},
        )
        return True

    async def step_segment_audio(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        manifest_path = dirs["outputs"] / "segments_manifest.json"
        if manifest_path.exists():
            return True
        if dry_run:
            return False

        audio_wav = self._transcribe_audio_path(dirs)
        if not audio_wav.exists():
            raise RuntimeError("Missing extracted WAV")

        duration = await asyncio.to_thread(probe_duration, audio_wav)
        silence_points = await asyncio.to_thread(
            detect_silence_points,
            audio_wav,
            dirs["logs"] / "task.log",
            self.settings.segment_search_window_seconds,
        )
        segments = build_segments(
            duration_sec=duration,
            target_seconds=self.settings.segment_target_seconds,
            search_window_seconds=self.settings.segment_search_window_seconds,
            overlap_seconds=self.settings.segment_overlap_seconds,
            silence_points=silence_points,
        )
        total_segments = len(segments)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="segment_progress",
            data={"current": 0, "total": total_segments},
        )
        loop = asyncio.get_running_loop()

        def sync_segment_progress(current: int, total: int) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event="segment_progress",
                        data={"current": int(current), "total": int(total)},
                        throttle_key="segment_progress",
                    )
                )
            )

        specs = await asyncio.to_thread(
            export_segments,
            audio_wav,
            segments,
            dirs["segments"],
            dirs["logs"] / "task.log",
            sync_segment_progress,
        )
        logger.info("segmentation finished with %s segments", len(specs))
        write_json(manifest_path, {"segments": specs})
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "segment_audio", "segments": len(specs)},
        )
        async with self.session_factory() as session:
            repo = Repo(session)
            await repo.clear_asr_for_task(task_id)
            for spec in specs:
                await repo.upsert_asr_segment_payload(
                    task_id=task_id,
                    segment_index=int(spec["segment_index"]),
                    start_sec=float(spec["start"]),
                    end_sec=float(spec["end"]),
                    text="",
                    raw_json={},
                )
            await session.commit()
        return True

    async def step_detect_language(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        marker = dirs["outputs"] / "language_detection.json"
        transcript_json = dirs["outputs"] / "transcript.json"
        explicit = self._normalize_language(task_options.get("language"))
        if explicit:
            task_options["detected_language"] = explicit
            if not marker.exists() and not dry_run:
                write_json(
                    marker,
                    {
                        "source": "task_option",
                        "language": explicit,
                        "confidence": 1.0,
                        "detected_at": utcnow().isoformat(),
                    },
                )
                if hasattr(self, "session_factory"):
                    await self._persist_detected_language(task_id, explicit, 1.0)
            return True

        if marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            language = self._normalize_language(payload.get("language") if isinstance(payload, dict) else None)
            confidence = payload.get("confidence") if isinstance(payload, dict) else None
            if (
                language
                and isinstance(confidence, (int, float))
                and float(confidence) >= self.settings.language_detection_confidence_threshold
            ):
                task_options["detected_language"] = language
                return True
        if dry_run and transcript_json.exists():
            return True
        if dry_run:
            return False

        manifest_path = dirs["outputs"] / "segments_manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Missing segment manifest for language detection")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        specs = payload.get("segments", [])
        if not isinstance(specs, list) or not specs:
            raise RuntimeError("Missing segment files for language detection")
        first = specs[0]
        segment_path = dirs["segments"] / str(first.get("file", ""))
        if not segment_path.exists():
            if transcript_json.exists():
                inferred = self._infer_language_from_transcript(transcript_json)
                if inferred:
                    task_options["detected_language"] = inferred
                    write_json(
                        marker,
                        {
                            "source": "resume_transcript_fallback",
                            "language": inferred,
                            "confidence": 0.5,
                            "threshold": self.settings.language_detection_confidence_threshold,
                            "detected_at": utcnow().isoformat(),
                        },
                    )
                logger.info(
                    "language detection fallback: first segment missing, transcript already exists, inferred=%s",
                    inferred,
                )
                await self.bus.publish_event(
                    user_id=user_id,
                    task_id=str(task_id),
                    event="phase",
                    data={"phase": "detect_language", "status": "done", "fallback": True, "language": inferred},
                )
                return True
            raise RuntimeError("Missing first segment for language detection")

        logger.info("waiting for heavy slot: detect language")
        async with self.heavy_slot:
            logger.info("heavy slot acquired: detect language")
            raw = await transcribe_with_whisper(
                whisper_url=self.settings.whisper_url,
                whisper_backend=self.settings.whisper_backend,
                audio_path=segment_path,
                language=None,
                initial_prompt=None,
            )
        self._log_payload(logger, "asr language probe response", raw)
        language, confidence = self._extract_detected_language(raw)
        threshold = self.settings.language_detection_confidence_threshold
        if not language:
            raise RuntimeError("Auto language detection failed: language not recognized")
        confidence_source = "whisper_payload"
        if confidence is None:
            confidence = float(threshold)
            confidence_source = "assumed_threshold"
            logger.warning(
                "language detection confidence is missing for language=%s; using threshold fallback=%.3f",
                language,
                confidence,
            )
        if confidence < threshold:
            raise RuntimeError(
                f"Auto language detection confidence too low: language={language}, "
                f"confidence={confidence}, threshold={threshold}"
            )
        task_options["detected_language"] = language
        write_json(
            marker,
            {
                "source": "whisper_first_segment",
                "language": language,
                "confidence": confidence,
                "confidence_source": confidence_source,
                "threshold": threshold,
                "detected_at": utcnow().isoformat(),
            },
        )
        await self._persist_detected_language(task_id, language, confidence)
        logger.info("language detected: %s (confidence=%.3f, source=%s)", language, confidence, confidence_source)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={
                "phase": "detect_language",
                "status": "done",
                "language": language,
                "confidence": confidence,
                "confidence_source": confidence_source,
            },
        )
        return True

    async def step_transcribe_segments(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        manifest_path = dirs["outputs"] / "segments_manifest.json"
        if not manifest_path.exists():
            if dry_run:
                return False
            raise RuntimeError("Missing segment manifest")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        specs: list[dict[str, Any]] = payload.get("segments", [])
        if not specs:
            return dry_run

        async with self.session_factory() as session:
            repo = Repo(session)
            existing_segments = {seg.segment_index: seg for seg in await repo.get_task_segments(task_id)}
        missing = []
        for spec in specs:
            idx = int(spec["segment_index"])
            seg = existing_segments.get(idx)
            if seg is None:
                missing.append(spec)
                continue
            if isinstance(seg.raw_json, dict) and seg.raw_json:
                continue
            missing.append(spec)
        if not missing:
            return True
        if dry_run:
            return False

        language = self._effective_language(task_options, dirs)
        if not language:
            raise RuntimeError("Missing transcription language after detection")
        missing.sort(key=lambda spec: int(spec["segment_index"]))
        text_by_index = {seg.segment_index: seg.text for seg in existing_segments.values() if seg.text.strip()}
        suspicious_by_index: dict[int, bool] = {
            seg.segment_index: self._is_probable_asr_hallucination(text=seg.text, words=[])
            for seg in existing_segments.values()
            if seg.text.strip()
        }
        whisper_url = self.settings.whisper_url
        whisper_backend = self.settings.whisper_backend
        for spec in missing:
            idx = int(spec["segment_index"])
            segment_path = dirs["segments"] / str(spec["file"])
            start = float(spec["start"])
            end = float(spec["end"])
            initial_prompt = None
            if not suspicious_by_index.get(idx - 1, False):
                initial_prompt = self._tail_prompt(text_by_index.get(idx - 1, ""))
            logger.info("waiting for heavy slot: transcribe segment %s", idx)
            _asr_retries = 0
            _t_asr_q0 = time.monotonic()
            async with self.heavy_slot:
                _t_asr_q_ms = round((time.monotonic() - _t_asr_q0) * 1000)
                logger.info("heavy slot acquired: transcribe segment %s", idx)
                _t_asr0 = time.monotonic()
                raw = await transcribe_with_whisper(
                    whisper_url=whisper_url,
                    whisper_backend=whisper_backend,
                    audio_path=segment_path,
                    language=language,
                    initial_prompt=initial_prompt,
                )
                _t_asr_ms = round((time.monotonic() - _t_asr0) * 1000)
            self._log_payload(logger, f"asr response segment={idx}", raw)
            text, words = normalize_whisper_output(raw, segment_offset_sec=start, whisper_backend=whisper_backend)
            suspicious = self._is_probable_asr_hallucination(text=text, words=words)
            if suspicious:
                _asr_retries = 1
                logger.warning("asr segment %s appears repetitive/noisy; retrying without tail prompt", idx)
                logger.info("waiting for heavy slot: transcribe segment %s retry", idx)
                async with self.heavy_slot:
                    logger.info("heavy slot acquired: transcribe segment %s retry", idx)
                    retry_raw = await transcribe_with_whisper(
                        whisper_url=whisper_url,
                        whisper_backend=whisper_backend,
                        audio_path=segment_path,
                        language=language,
                        initial_prompt=None,
                    )
                retry_text, retry_words = normalize_whisper_output(retry_raw, segment_offset_sec=start, whisper_backend=whisper_backend)
                retry_suspicious = self._is_probable_asr_hallucination(text=retry_text, words=retry_words)
                old_score = self._transcript_quality_score(text, words)
                new_score = self._transcript_quality_score(retry_text, retry_words)
                if (not retry_suspicious) or (new_score > old_score):
                    raw = retry_raw
                    text = retry_text
                    words = retry_words
                    suspicious = retry_suspicious
                    logger.info(
                        "asr retry accepted for segment %s (old_score=%.3f, new_score=%.3f)",
                        idx,
                        old_score,
                        new_score,
                    )
                else:
                    logger.info(
                        "asr retry rejected for segment %s (old_score=%.3f, new_score=%.3f)",
                        idx,
                        old_score,
                        new_score,
                    )
            suspicious_by_index[idx] = suspicious
            _asr_dur_s = end - start
            _asr_rtf = (_t_asr_ms / 1000.0) / _asr_dur_s if _asr_dur_s > 0 else None
            _asr_em = self._get_emitter(task_id)
            if _asr_em:
                _asr_em.emit({
                    "stage": "transcribe.segment",
                    "status": "ok",
                    "segment_id": idx,
                    "audio_start_s": round(start, 3),
                    "audio_end_s": round(end, 3),
                    "audio_duration_s": round(_asr_dur_s, 3),
                    "t_wall_ms": _t_asr_ms,
                    "t_queue_ms": _t_asr_q_ms,
                    "rtf": round(_asr_rtf, 4) if _asr_rtf is not None else None,
                    "retries": _asr_retries,
                    "whisper_backend": whisper_backend,
                    "artifacts": {"segment_file": str(spec.get("file", ""))},
                })
            text_by_index[idx] = text
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="transcribe_progress",
                data={"segment_index": idx, "total": len(specs)},
                throttle_key="transcribe_progress",
            )
            async with self.session_factory() as session:
                repo = Repo(session)
                seg = await repo.upsert_asr_segment_payload(
                    task_id=task_id,
                    segment_index=idx,
                    start_sec=start,
                    end_sec=end,
                    text=text,
                    raw_json=raw,
                )
                await repo.replace_asr_words(task_id=task_id, segment_id=seg.id, words=words)
                await session.commit()
            await asyncio.sleep(self.settings.services_database_write_throttle_ms / 1000.0)

        async with self.session_factory() as session:
            repo = Repo(session)
            all_segments = await repo.get_task_segments(task_id)
        asr_dir = dirs["root"] / "asr"
        asr_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            asr_dir / "segments_raw.json",
            {
                "segments": [
                    {
                        "segment_index": int(seg.segment_index),
                        "start": float(seg.start_sec),
                        "end": float(seg.end_sec),
                        "raw_json": seg.raw_json,
                    }
                    for seg in all_segments
                    if isinstance(seg.raw_json, dict) and bool(seg.raw_json)
                ]
            },
        )
        return True

    async def step_merge_transcript(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        task_options: dict[str, Any],
        dry_run: bool,
    ) -> bool:
        transcript_json = dirs["outputs"] / "transcript.json"
        transcript_txt = dirs["outputs"] / "transcript.txt"
        if transcript_json.exists() and transcript_txt.exists():
            return True
        if dry_run:
            return False

        async with self.session_factory() as session:
            repo = Repo(session)
            segments = await repo.get_task_segments(task_id)
            words_by_segment = await repo.get_all_asr_words_for_task(task_id)
            entries: list[dict[str, Any]] = []
            merged_tokens: list[str] = []
            previous_segment_end = -1.0
            for segment in segments:
                words = words_by_segment.get(segment.id, [])
                if words:
                    for word in words:
                        if word.start_sec < previous_segment_end:
                            continue
                        token = word.word.strip()
                        if not token:
                            continue
                        merged_tokens.append(token)
                        entries.append({"start": word.start_sec, "end": word.end_sec, "word": token})
                else:
                    fallback_text = segment.text.strip()
                    if fallback_text and segment.start_sec >= previous_segment_end:
                        merged_tokens.append(fallback_text)
                        entries.append({"start": segment.start_sec, "end": segment.end_sec, "text": fallback_text})
                previous_segment_end = max(previous_segment_end, segment.end_sec)
            merged_text = " ".join(token for token in merged_tokens if token).strip()
            cleaned_text, cleanup_meta = self._trim_repetitive_edges(merged_text)
            write_json(
                transcript_json,
                {
                    "text": cleaned_text,
                    "raw_text": merged_text,
                    "entries": entries,
                    "cleanup": cleanup_meta,
                },
            )
            transcript_txt.write_text(cleaned_text, encoding="utf-8")
            logger.info(
                "transcript merge cleanup: start_removed=%s end_removed=%s",
                cleanup_meta.get("removed_head_sentences", 0),
                cleanup_meta.get("removed_tail_sentences", 0),
            )

            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("task not found during merge")
            task.transcript_path = str(transcript_txt)
            await session.commit()

        for path in dirs["segments"].glob("*.wav"):
            path.unlink(missing_ok=True)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "merge_transcript", "status": "done"},
        )
        return True

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
        target_model = self.settings.llama_model
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
            logger.info("waiting for heavy slot: llama warmup")
            async with self.heavy_slot:
                logger.info("heavy slot acquired: llama warmup")
                raw = await llama_chat_completion(
                    llama_url=self.settings.llama_url,
                    model=target_model,
                    system_prompt='Return compact JSON: {"status":"ready"}.',
                    user_prompt="Warm up model for upcoming summarization.",
                    timeout_seconds=1200,
                    max_tokens=32,
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
            write_json(chunks_file, {"chunks": []})
            write_json(dirs["outputs"] / "summary_chunks.json", {"chunks": []})
            return True

        logger.info("summary chunk preparation started")
        chunks = await chunk_text(
            text=transcript,
            llama_url=self.settings.llama_url,
            model=self.settings.llama_model,
            window_tokens=2000,
            overlap_ratio=0.15,
        )
        logger.info("summary chunk preparation finished: %s windows", len(chunks))
        write_json(chunks_file, {"chunks": chunks})
        write_json(dirs["outputs"] / "summary_chunks.json", {"chunks": chunks})
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
            load_prompt(
                self.settings.prompts_dir,
                "segment_prompt.md",
                "Return JSON with keys: topic, bullets, action_items.",
            ),
            output_language,
        )
        chunks_file = summary_dir / "chunks.json"
        if not chunks_file.exists():
            chunks_file = dirs["outputs"] / "summary_chunks.json"
        if not chunks_file.exists():
            raise RuntimeError("Missing summary chunks")
        chunks = json.loads(chunks_file.read_text(encoding="utf-8")).get("chunks", [])
        if not isinstance(chunks, list):
            raise RuntimeError("Invalid summary chunks payload")
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
            logger.info("window summaries already complete: %s", total_windows)
            return True

        logger.info("window summarization started: %s windows", len(chunks))
        budget_cfg = self._token_budget_config()
        total_parts = len(chunks) + 1
        timeout_seconds = int(getattr(self.settings, "llama_chat_timeout_seconds", 600))
        for idx, chunk in enumerate(chunks, start=1):
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
            input_tokens = await count_tokens(
                text=user_prompt,
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                timeout_seconds=timeout_seconds,
            )
            target_tokens, min_out, max_out = compute_segment_budget(input_tokens, budget_cfg)
            budgeted_prompt = self._render_prompt_budget_vars(
                segment_prompt,
                input_tokens=input_tokens,
                target_tokens=target_tokens,
            )
            logger.info(
                "window %s/%s token_budget input=%d target=%d min=%d max=%d",
                idx, len(chunks), input_tokens, target_tokens, min_out, max_out,
            )
            logger.info("waiting for heavy slot: summarize window %s/%s", idx, len(chunks))
            _win_t_q0 = time.monotonic()
            async with self.heavy_slot:
                _win_t_q_ms = round((time.monotonic() - _win_t_q0) * 1000)
                logger.info("heavy slot acquired: summarize window %s/%s", idx, len(chunks))
                _win_t0 = time.monotonic()
                raw = await llama_chat_completion(
                    llama_url=self.settings.llama_url,
                    model=self.settings.llama_model,
                    system_prompt=budgeted_prompt,
                    user_prompt=user_prompt,
                    timeout_seconds=timeout_seconds,
                    max_tokens=target_tokens,
                    use_json_format=False,
                )
                _win_t_ms = round((time.monotonic() - _win_t0) * 1000)
            actual_output_tokens = await count_tokens(
                text=raw,
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                timeout_seconds=timeout_seconds,
            )
            self._log_metrics(logger, SummarizationMetrics(
                stage_name="segment",
                input_tokens=input_tokens,
                target_tokens=target_tokens,
                actual_output_tokens=actual_output_tokens,
            ))
            self._log_payload(logger, f"llm window response index={idx}", raw)
            _win_em = self._get_emitter(task_id)
            if _win_em:
                _n_ctx = self.settings.summary_n_ctx
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
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="summary_progress",
                data={"current": idx, "total": total_parts},
                throttle_key="summary_progress",
            )
            await self._persist_summary_progress(task_id, idx, total_parts)
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

        If the total notes tokens already fit within final_in_budget, the step
        is a no-op (writes the passthrough marker and exits).  Otherwise it
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
        timeout_seconds = int(getattr(self.settings, "llama_final_timeout_seconds", 1800))
        budget_cfg = self._token_budget_config()

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
        final_prompt_tokens = await count_tokens(
            text=final_prompt_text,
            llama_url=self.settings.llama_url,
            model=self.settings.llama_model,
            timeout_seconds=timeout_seconds,
        )
        final_in_budget = compute_final_in_budget(budget_cfg, final_prompt_tokens)
        logger.info(
            "pack_window_notes: final_prompt_tokens=%d final_in_budget=%d",
            final_prompt_tokens,
            final_in_budget,
        )

        # Count total tokens of all notes
        notes_texts: list[str] = [self._extract_window_text(w) for w in windows]
        note_token_counts: list[int] = []
        for text in notes_texts:
            tc = await count_tokens(
                text=text,
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                timeout_seconds=timeout_seconds,
            )
            note_token_counts.append(tc)
        total_notes_tokens = sum(note_token_counts)

        logger.info(
            "pack_window_notes: total_notes_tokens=%d final_in_budget=%d packing_needed=%s",
            total_notes_tokens,
            final_in_budget,
            total_notes_tokens > final_in_budget,
        )

        packing_triggered = total_notes_tokens > final_in_budget
        packing_pass_count = 0

        if packing_triggered:
            pack_prompt_template = self._render_prompt_with_language(
                load_prompt(
                    self.settings.prompts_dir,
                    "pack_prompt.md",
                    "Integrate and deduplicate the following notes. "
                    "Target output: ${TARGET_TOKENS} tokens (input: ${INPUT_TOKENS} tokens).\n"
                    "Output language: ${LANG}.",
                ),
                output_language,
            )

            current_texts = notes_texts
            current_token_counts = note_token_counts

            while total_notes_tokens > final_in_budget and len(current_texts) > 0:
                packing_pass_count += 1
                logger.info(
                    "packing pass %d: total_tokens=%d budget=%d notes=%d",
                    packing_pass_count,
                    total_notes_tokens,
                    final_in_budget,
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
                    batch_input = "\n\n".join(batch)
                    batch_input_tokens = await count_tokens(
                        text=batch_input,
                        llama_url=self.settings.llama_url,
                        model=self.settings.llama_model,
                        timeout_seconds=timeout_seconds,
                    )
                    target_tokens, min_out, max_out = compute_pack_budget(
                        batch_input_tokens, budget_cfg
                    )
                    pack_system_prompt = self._render_prompt_budget_vars(
                        pack_prompt_template,
                        input_tokens=batch_input_tokens,
                        target_tokens=target_tokens,
                    )
                    logger.info(
                        "pack batch %d/%d: input=%d target=%d min=%d max=%d",
                        b_idx, len(batches), batch_input_tokens, target_tokens, min_out, max_out,
                    )
                    async with self.heavy_slot:
                        packed_text = await llama_chat_completion(
                            llama_url=self.settings.llama_url,
                            model=self.settings.llama_model,
                            system_prompt=pack_system_prompt,
                            user_prompt=batch_input,
                            timeout_seconds=timeout_seconds,
                            max_tokens=target_tokens,
                            use_json_format=False,
                        )
                    packed_tc = await count_tokens(
                        text=packed_text,
                        llama_url=self.settings.llama_url,
                        model=self.settings.llama_model,
                        timeout_seconds=timeout_seconds,
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
                if len(current_texts) == 1 and total_notes_tokens > final_in_budget:
                    logger.warning(
                        "packing converged to a single note but still exceeds budget "
                        "(%d > %d); proceeding anyway",
                        total_notes_tokens,
                        final_in_budget,
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
                "final_in_budget": final_in_budget,
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

    async def step_summarize_final(
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
        summary_json = summary_dir / "final.json"
        summary_md = summary_dir / "final.md"
        if summary_json.exists() and summary_md.exists():
            async with self.session_factory() as session:
                repo = Repo(session)
                task = await repo.get_task_by_id(task_id)
                if task is None:
                    raise RuntimeError("task not found during final summary restore")
                summary_path = str(summary_md)
                if task.summary_path != summary_path:
                    task.summary_path = summary_path
                    await session.commit()
            return True
        if dry_run:
            return False

        output_language = self._effective_language(task_options, dirs)
        timeout_seconds = int(getattr(self.settings, "llama_final_timeout_seconds", 1800))
        budget_cfg = self._token_budget_config()

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

        global_prompt_base = self._render_prompt_with_language(
            load_prompt(
                self.settings.prompts_dir,
                "global_prompt.md",
                "Produce a structured knowledge document from the notes.\n\nOutput language: ${LANG}.",
            ),
            output_language,
        )
        # Stage C: adaptive token budget
        input_tokens = await count_tokens(
            text=merged,
            llama_url=self.settings.llama_url,
            model=self.settings.llama_model,
            timeout_seconds=timeout_seconds,
        )
        target_tokens, min_out, max_out = compute_final_budget(input_tokens, budget_cfg)
        final_prompt_tokens = await count_tokens(
            text=global_prompt_base,
            llama_url=self.settings.llama_url,
            model=self.settings.llama_model,
            timeout_seconds=timeout_seconds,
        )
        final_in_budget = compute_final_in_budget(budget_cfg, final_prompt_tokens)
        global_prompt = self._render_prompt_budget_vars(
            global_prompt_base,
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            final_in_budget=final_in_budget,
            final_out_budget=budget_cfg.final_out_budget,
        )
        logger.info(
            "final summary token_budget input=%d target=%d min=%d max=%d final_in=%d final_out=%d",
            input_tokens, target_tokens, min_out, max_out, final_in_budget,
            budget_cfg.final_out_budget,
        )
        logger.info(
            "waiting for heavy slot: final summary (notes=%s payload_bytes=%s)",
            total_windows,
            len(merged.encode("utf-8")),
        )
        _fin_t_q0 = time.monotonic()
        async with self.heavy_slot:
            _fin_t_q_ms = round((time.monotonic() - _fin_t_q0) * 1000)
            logger.info("heavy slot acquired: final summary")
            _fin_t0 = time.monotonic()
            raw = await llama_chat_completion(
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                system_prompt=global_prompt,
                user_prompt=merged,
                timeout_seconds=timeout_seconds,
                max_tokens=target_tokens,
                use_json_format=False,
            )
            _fin_t_ms = round((time.monotonic() - _fin_t0) * 1000)

        actual_output_tokens = await count_tokens(
            text=raw,
            llama_url=self.settings.llama_url,
            model=self.settings.llama_model,
            timeout_seconds=timeout_seconds,
        )
        self._log_metrics(logger, SummarizationMetrics(
            stage_name="final",
            input_tokens=input_tokens,
            target_tokens=target_tokens,
            actual_output_tokens=actual_output_tokens,
            packing_triggered=packing_triggered,
            packing_pass_count=packing_pass_count,
        ))
        self._log_payload(logger, "llm final summary response", raw)
        _fin_em = self._get_emitter(task_id)
        if _fin_em:
            _n_ctx = self.settings.summary_n_ctx
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
        write_json(dirs["outputs"] / "summary.json", {"raw": raw})
        summary_md.write_text(raw, encoding="utf-8")
        (dirs["outputs"] / "summary.md").write_text(raw, encoding="utf-8")
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="summary_progress",
            data={"current": total_parts, "total": total_parts},
        )
        await self._persist_summary_progress(task_id, total_parts, total_parts)
        logger.info("final summary generated")

        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("task not found during final summary")
            task.summary_path = str(summary_md)
            await session.commit()
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

    def _transcribe_audio_path(self, dirs: dict[str, Path]) -> Path:
        trimmed = dirs["media"] / "audio_16k_trimmed.wav"
        if trimmed.exists():
            return trimmed
        return dirs["media"] / "audio_16k.wav"

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

    def _infer_language_from_transcript(self, transcript_json: Path) -> str | None:
        try:
            payload = json.loads(transcript_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        cyr = sum(1 for ch in text if ("\u0400" <= ch <= "\u04ff"))
        lat = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
        if cyr == 0 and lat == 0:
            return None
        return "ru" if cyr >= lat else "en"

    def _extract_detected_language(self, payload: dict[str, Any]) -> tuple[str | None, float | None]:
        language = self._normalize_language(payload.get("language"))
        confidence_raw = payload.get("language_probability")
        if confidence_raw is None:
            confidence_raw = payload.get("language_confidence")
        if confidence_raw is None:
            # whisper.cpp verbose_json field
            confidence_raw = payload.get("detected_language_probability")
        if confidence_raw is None:
            language_probs = payload.get("language_probs")
            if isinstance(language_probs, dict) and language:
                confidence_raw = language_probs.get(language)
        if confidence_raw is None:
            # whisper.cpp language_probabilities map
            lang_prob_map = payload.get("language_probabilities")
            if isinstance(lang_prob_map, dict) and language:
                confidence_raw = lang_prob_map.get(language)
        if confidence_raw is None:
            confidence_raw = self._extract_confidence_from_word_probabilities(payload)
        confidence: float | None
        if isinstance(confidence_raw, (int, float)):
            confidence = float(confidence_raw)
        else:
            confidence = None
        return language, confidence

    def _extract_confidence_from_word_probabilities(self, payload: dict[str, Any]) -> float | None:
        segments = payload.get("segments")
        if not isinstance(segments, list):
            return None
        probabilities: list[float] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            words = segment.get("words")
            if not isinstance(words, list):
                continue
            for word in words:
                if not isinstance(word, dict):
                    continue
                probability = word.get("probability")
                if isinstance(probability, (int, float)):
                    value = float(probability)
                    if 0.0 <= value <= 1.0:
                        probabilities.append(value)
                if len(probabilities) >= 64:
                    break
            if len(probabilities) >= 64:
                break
        if not probabilities:
            return None
        return sum(probabilities) / float(len(probabilities))

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

    def _is_probable_asr_hallucination(self, *, text: str, words: list[dict[str, Any]]) -> bool:
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized:
            return False
        tokens = [self._normalize_token(token) for token in re.split(r"\s+", normalized)]
        tokens = [token for token in tokens if token]
        if len(tokens) < 10:
            return False

        token_counts = Counter(tokens)
        unique_ratio = len(token_counts) / float(len(tokens))
        top_token_ratio = token_counts.most_common(1)[0][1] / float(len(tokens))

        sentences = [self._normalize_token(part) for part in re.split(r"[.!?…]+", normalized)]
        sentences = [sentence for sentence in sentences if sentence]
        repeated_edge = False
        if len(sentences) >= 5:
            head = sentences[0]
            tail = sentences[-1]
            head_repeats = 0
            tail_repeats = 0
            for sentence in sentences:
                if sentence == head:
                    head_repeats += 1
                else:
                    break
            for sentence in reversed(sentences):
                if sentence == tail:
                    tail_repeats += 1
                else:
                    break
            repeated_edge = max(head_repeats, tail_repeats) >= 5

        low_confidence_ratio = 0.0
        if words:
            confidences = [float(item.get("confidence")) for item in words if item.get("confidence") is not None]
            if confidences:
                low_confidence_ratio = sum(1 for value in confidences if value < 0.35) / float(len(confidences))

        signals = 0
        if unique_ratio < 0.30:
            signals += 1
        if top_token_ratio > 0.35:
            signals += 1
        if repeated_edge:
            signals += 1
        if low_confidence_ratio > 0.75:
            signals += 1
        return signals >= 2

    def _transcript_quality_score(self, text: str, words: list[dict[str, Any]]) -> float:
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized:
            return 0.0
        tokens = [self._normalize_token(token) for token in re.split(r"\s+", normalized)]
        tokens = [token for token in tokens if token]
        if not tokens:
            return 0.0
        unique_ratio = len(set(tokens)) / float(len(tokens))
        score = len(tokens) * unique_ratio
        if words:
            confidences = [float(item.get("confidence")) for item in words if item.get("confidence") is not None]
            if confidences:
                score += sum(confidences) / float(len(confidences)) * 10.0
        return score

    def _normalize_token(self, value: str) -> str:
        return re.sub(r"[^\wа-яА-ЯёЁ]+", "", value, flags=re.UNICODE).strip().lower()

    def _trim_repetitive_edges(self, text: str) -> tuple[str, dict[str, Any]]:
        if not text.strip():
            return "", {
                "removed_head_sentences": 0,
                "removed_tail_sentences": 0,
                "head_phrase": None,
                "tail_phrase": None,
            }
        raw_sentences = [item.strip() for item in re.split(r"(?<=[.!?…])\s+", text) if item.strip()]
        if not raw_sentences:
            return text.strip(), {
                "removed_head_sentences": 0,
                "removed_tail_sentences": 0,
                "head_phrase": None,
                "tail_phrase": None,
            }

        sentences = list(raw_sentences)
        removed_head = 0
        removed_tail = 0
        head_phrase: str | None = None
        tail_phrase: str | None = None
        min_repeats = 6

        while len(sentences) >= min_repeats:
            head = self._normalize_token(sentences[0])
            if not head or len(head) > 64:
                break
            repeats = 0
            for sentence in sentences:
                if self._normalize_token(sentence) == head:
                    repeats += 1
                else:
                    break
            if repeats < min_repeats:
                break
            removed_head += repeats
            head_phrase = sentences[0]
            sentences = sentences[repeats:]

        while len(sentences) >= min_repeats:
            tail = self._normalize_token(sentences[-1])
            if not tail or len(tail) > 64:
                break
            repeats = 0
            for sentence in reversed(sentences):
                if self._normalize_token(sentence) == tail:
                    repeats += 1
                else:
                    break
            if repeats < min_repeats:
                break
            removed_tail += repeats
            tail_phrase = sentences[-1]
            sentences = sentences[:-repeats]

        cleaned = " ".join(sentences).strip()
        if not cleaned:
            cleaned = text.strip()
        return cleaned, {
            "removed_head_sentences": removed_head,
            "removed_tail_sentences": removed_tail,
            "head_phrase": head_phrase,
            "tail_phrase": tail_phrase,
        }

    def _task_options(self, raw_options: dict[str, Any] | None) -> dict[str, Any]:
        return dict(raw_options or {})

    def _task_flag(self, options: dict[str, Any], key: str, *, default: bool) -> bool:
        value = options.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _is_step_enabled(self, step_name: str, task_options: dict[str, Any]) -> bool:
        transcript_enabled = self._task_flag(task_options, "transcript", default=True)
        summary_enabled = self._task_flag(task_options, "summary", default=True)
        if not transcript_enabled:
            return step_name == "download"
        if not summary_enabled and step_name in {
            "prepare_llama_model",
            "prepare_summary_chunks",
            "summarize_windows",
            "summarize_final",
        }:
            return False
        return True

    def _tail_prompt(self, text: str, max_chars: int = 800) -> str | None:
        normalized = text.strip()
        if not normalized:
            return None
        return normalized[-max_chars:]

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
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        return logger

    async def delete_task_artifacts(self, artifact_dir: str) -> None:
        await asyncio.to_thread(shutil.rmtree, artifact_dir, True)

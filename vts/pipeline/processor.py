from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vts.core.config import Settings
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
from vts.services.summarizer import (
    chunk_text,
    llama_chat_completion,
    load_prompt,
    parse_json_response,
)
from vts.services.transcription import normalize_whisper_output, transcribe_with_whisper


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

    async def process_task(self, task_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return
            if task.status in {TaskStatus.canceled, TaskStatus.completed}:
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
                    await asyncio.sleep(self.settings.db_write_throttle_ms / 1000.0)
                await self._cleanup_media(dirs["media"])
                await repo.set_task_status(task, TaskStatus.completed)
                await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": task.status.value},
                )
            except Exception as exc:
                logger.exception("pipeline failed: %s", exc)
                await repo.set_task_status(task, TaskStatus.failed, error_message=str(exc))
                await session.commit()
                await self.bus.publish_event(
                    user_id=str(task.user_id),
                    task_id=str(task.id),
                    event="task_status",
                    data={"status": TaskStatus.failed.value, "error": str(exc)},
                )

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
        try:
            await method(task_id, user_id, dirs, logger, task_options, dry_run=False)
            await repo.set_step_status(step, StepStatus.completed)
            await session.commit()
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="step",
                data={"name": step_name, "status": StepStatus.completed.value},
            )
        except Exception as exc:
            await repo.set_step_status(step, StepStatus.failed, message=str(exc))
            await session.commit()
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="step",
                data={"name": step_name, "status": StepStatus.failed.value, "error": str(exc)},
            )
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
            return False

        source_url = await self._task_url(task_id)
        user_uuid = uuid.UUID(user_id)
        preferred_youtube_client = await self._get_user_preferred_ytdlp_client(user_uuid)
        if preferred_youtube_client:
            logger.info("using saved yt-dlp youtube client for user: %s", preferred_youtube_client)
        loop = asyncio.get_running_loop()

        def sync_progress(phase: str, payload: dict[str, Any]) -> None:
            event = "video_progress" if phase == "video" else "audio_progress"
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.bus.publish_event(
                        user_id=user_id,
                        task_id=str(task_id),
                        event=event,
                        data=payload,
                        throttle_key=event,
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
        if output.exists():
            return True
        if dry_run:
            return False
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
        specs = await asyncio.to_thread(
            export_segments,
            audio_wav,
            segments,
            dirs["segments"],
            dirs["logs"] / "task.log",
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
            raise RuntimeError("Missing first segment for language detection")

        logger.info("waiting for heavy slot: detect language")
        async with self.heavy_slot:
            logger.info("heavy slot acquired: detect language")
            raw = await transcribe_with_whisper(
                whisper_url=self.settings.whisper_url,
                audio_path=segment_path,
                language=None,
                initial_prompt=None,
            )
        self._log_payload(logger, "asr language probe response", raw)
        language, confidence = self._extract_detected_language(raw)
        threshold = self.settings.language_detection_confidence_threshold
        if not language:
            raise RuntimeError("Auto language detection failed: language not recognized")
        if confidence is None or confidence < threshold:
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
                "threshold": threshold,
                "detected_at": utcnow().isoformat(),
            },
        )
        await self._persist_detected_language(task_id, language, confidence)
        logger.info("language detected: %s (confidence=%.3f)", language, confidence)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "detect_language", "status": "done", "language": language, "confidence": confidence},
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
        for spec in missing:
            idx = int(spec["segment_index"])
            segment_path = dirs["segments"] / str(spec["file"])
            start = float(spec["start"])
            end = float(spec["end"])
            initial_prompt = None
            if not suspicious_by_index.get(idx - 1, False):
                initial_prompt = self._tail_prompt(text_by_index.get(idx - 1, ""))
            logger.info("waiting for heavy slot: transcribe segment %s", idx)
            async with self.heavy_slot:
                logger.info("heavy slot acquired: transcribe segment %s", idx)
                raw = await transcribe_with_whisper(
                    whisper_url=self.settings.whisper_url,
                    audio_path=segment_path,
                    language=language,
                    initial_prompt=initial_prompt,
                )
            self._log_payload(logger, f"asr response segment={idx}", raw)
            text, words = normalize_whisper_output(raw, segment_offset_sec=start)
            suspicious = self._is_probable_asr_hallucination(text=text, words=words)
            if suspicious:
                logger.warning("asr segment %s appears repetitive/noisy; retrying without tail prompt", idx)
                logger.info("waiting for heavy slot: transcribe segment %s retry", idx)
                async with self.heavy_slot:
                    logger.info("heavy slot acquired: transcribe segment %s retry", idx)
                    retry_raw = await transcribe_with_whisper(
                        whisper_url=self.settings.whisper_url,
                        audio_path=segment_path,
                        language=language,
                        initial_prompt=None,
                    )
                retry_text, retry_words = normalize_whisper_output(retry_raw, segment_offset_sec=start)
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
            await asyncio.sleep(self.settings.db_write_throttle_ms / 1000.0)

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
            entries: list[dict[str, Any]] = []
            merged_tokens: list[str] = []
            previous_segment_end = -1.0
            for segment in segments:
                words = await repo.get_words_for_segment(segment.id)
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
                    if not isinstance(summary_payload, dict):
                        summary_payload = {"raw": str(summary_payload)}
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
            try:
                parsed = json.loads(window_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            windows_by_index[idx] = {
                "window_index": idx,
                "summary": parsed,
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
        total_parts = len(chunks) + 1
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
                continue
            logger.info("summarizing window %s/%s", idx, len(chunks))
            logger.info("waiting for heavy slot: summarize window %s/%s", idx, len(chunks))
            async with self.heavy_slot:
                logger.info("heavy slot acquired: summarize window %s/%s", idx, len(chunks))
                raw = await llama_chat_completion(
                    llama_url=self.settings.llama_url,
                    model=self.settings.llama_model,
                    system_prompt=segment_prompt,
                    user_prompt=f"Window {idx}/{len(chunks)}\n\n{chunk}",
                    timeout_seconds=int(getattr(self.settings, "llama_chat_timeout_seconds", 600)),
                )
            self._log_payload(logger, f"llm window response index={idx}", raw)
            parsed = parse_json_response(raw)
            window_path = summary_dir / f"window_{idx:02d}.txt"
            window_path.write_text(json.dumps(parsed, ensure_ascii=True, indent=2), encoding="utf-8")
            windows_by_index[idx] = {"window_index": idx, "summary": parsed, "path": str(window_path)}
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
        ordered = [windows_by_index[idx] for idx in sorted(windows_by_index)]
        write_json(output, {"windows": ordered})
        write_json(output_mirror, {"windows": ordered})
        logger.info("window summaries generated: %s", len(ordered))
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

        windows_file = summary_dir / "windows.json"
        if not windows_file.exists():
            windows_file = dirs["outputs"] / "window_summaries.json"
        if not windows_file.exists():
            raise RuntimeError("Missing window summaries")
        windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
        if not isinstance(windows, list):
            raise RuntimeError("Invalid window summaries payload")
        total_parts = len(windows) + 1
        logger.info("final summary generation started: windows=%s", len(windows))
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="summary_progress",
            data={"current": len(windows), "total": total_parts},
        )
        output_language = self._effective_language(task_options, dirs)
        global_prompt = self._render_prompt_with_language(
            load_prompt(
                self.settings.prompts_dir,
                "global_prompt.md",
                "Produce JSON with executive_summary, key_points, risks, decisions.",
            ),
            output_language,
        )
        merged = json.dumps(windows, ensure_ascii=True)
        logger.info("waiting for heavy slot: final summary")
        async with self.heavy_slot:
            logger.info("heavy slot acquired: final summary")
            raw = await llama_chat_completion(
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                system_prompt=global_prompt,
                user_prompt=merged,
                timeout_seconds=int(getattr(self.settings, "llama_final_timeout_seconds", 1800)),
            )
        self._log_payload(logger, "llm final summary response", raw)
        parsed = parse_json_response(raw)
        write_json(summary_json, parsed)
        write_json(dirs["outputs"] / "summary.json", parsed)
        summary_md.write_text(self._summary_markdown(parsed), encoding="utf-8")
        (dirs["outputs"] / "summary.md").write_text(self._summary_markdown(parsed), encoding="utf-8")
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="summary_progress",
            data={"current": total_parts, "total": total_parts},
        )
        logger.info("final summary generated")

        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("task not found during final summary")
            task.summary_path = str(summary_md)
            await session.commit()
        return True

    def _summary_markdown(self, payload: dict[str, Any]) -> str:
        lines = ["# Summary", ""]
        for key, value in payload.items():
            lines.append(f"## {key}")
            if isinstance(value, list):
                for item in value:
                    lines.append(f"- {item}")
            else:
                lines.append(str(value))
            lines.append("")
        return "\n".join(lines).strip() + "\n"

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

    def _extract_detected_language(self, payload: dict[str, Any]) -> tuple[str | None, float | None]:
        language = self._normalize_language(payload.get("language"))
        confidence_raw = payload.get("language_probability")
        if confidence_raw is None:
            confidence_raw = payload.get("language_confidence")
        if confidence_raw is None:
            language_probs = payload.get("language_probs")
            if isinstance(language_probs, dict) and language:
                confidence_raw = language_probs.get(language)
        confidence: float | None
        if isinstance(confidence_raw, (int, float)):
            confidence = float(confidence_raw)
        else:
            confidence = None
        return language, confidence

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

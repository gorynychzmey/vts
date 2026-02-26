from __future__ import annotations

import asyncio
import json
import logging
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
                    await self._run_step(session, repo, task.id, str(task.user_id), step_name, dirs, logger)
                    await session.refresh(task)
                    await asyncio.sleep(self.settings.db_write_throttle_ms / 1000.0)
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
    ) -> None:
        step = await repo.upsert_step(task_id, step_name)
        method = getattr(self, f"step_{step_name}")
        if step.status == StepStatus.completed and await method(task_id, user_id, dirs, logger, dry_run=True):
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
            await method(task_id, user_id, dirs, logger, dry_run=False)
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
        dry_run: bool,
    ) -> bool:
        video_file = next(dirs["media"].glob("video.*"), None)
        audio_file = next(dirs["media"].glob("audio.*"), None)
        if video_file and audio_file:
            return True
        if dry_run:
            return False

        source_url = await self._task_url(task_id)
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

        await asyncio.to_thread(
            download_video_and_audio,
            source_url=source_url,
            media_dir=dirs["media"],
            progress_cb=sync_progress,
            logger=logger,
        )
        logger.info("download finished")
        return True

    async def step_extract_audio(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        dry_run: bool,
    ) -> bool:
        output = dirs["media"] / "audio_16k.wav"
        if output.exists():
            return True
        if dry_run:
            return False
        audio_file = next(dirs["media"].glob("audio.*"), None)
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

    async def step_segment_audio(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        dry_run: bool,
    ) -> bool:
        manifest_path = dirs["outputs"] / "segments_manifest.json"
        if manifest_path.exists():
            return True
        if dry_run:
            return False

        audio_wav = dirs["media"] / "audio_16k.wav"
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
            await session.commit()
        return True

    async def step_transcribe_segments(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
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
            existing = {
                item.segment_index for item in await repo.get_task_segments(task_id)
            }
        missing = [spec for spec in specs if int(spec["segment_index"]) not in existing]
        if not missing:
            return True
        if dry_run:
            return False

        language = await self._task_language(task_id)
        semaphore = asyncio.Semaphore(self.settings.transcribe_parallel_per_task)

        async def process_one(spec: dict[str, Any]) -> dict[str, Any]:
            idx = int(spec["segment_index"])
            segment_path = dirs["segments"] / str(spec["file"])
            start = float(spec["start"])
            end = float(spec["end"])
            async with semaphore:
                async with self.heavy_slot:
                    logger.info("transcribing segment %s", idx)
                    raw = await transcribe_with_whisper(
                        whisper_url=self.settings.whisper_url,
                        audio_path=segment_path,
                        language=language,
                    )
            text, words = normalize_whisper_output(raw, segment_offset_sec=start)
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="transcribe_progress",
                data={"segment_index": idx, "total": len(specs)},
                throttle_key="transcribe_progress",
            )
            return {
                "segment_index": idx,
                "start": start,
                "end": end,
                "text": text,
                "raw_json": raw,
                "words": words,
            }

        results = await asyncio.gather(*(process_one(spec) for spec in missing))
        results.sort(key=lambda item: int(item["segment_index"]))
        async with self.session_factory() as session:
            repo = Repo(session)
            for item in results:
                if await repo.has_segment(task_id, int(item["segment_index"])):
                    continue
                seg = await repo.add_asr_segment(
                    task_id=task_id,
                    segment_index=int(item["segment_index"]),
                    start_sec=float(item["start"]),
                    end_sec=float(item["end"]),
                    text=str(item["text"]),
                    raw_json=item["raw_json"],
                )
                await repo.add_asr_words(task_id=task_id, segment_id=seg.id, words=item["words"])
                await session.flush()
                await asyncio.sleep(self.settings.db_write_throttle_ms / 1000.0)
            await session.commit()
        return True

    async def step_merge_transcript(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
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
            words = await repo.get_task_words(task_id)
            if not words:
                segments = await repo.get_task_segments(task_id)
                merged_text = " ".join(seg.text for seg in segments).strip()
                entries = [
                    {"start": seg.start_sec, "end": seg.end_sec, "text": seg.text}
                    for seg in segments
                ]
            else:
                entries: list[dict[str, Any]] = []
                merged_tokens: list[str] = []
                last_time = -1.0
                for word in words:
                    if word.start_sec <= last_time and merged_tokens and merged_tokens[-1] == word.word:
                        continue
                    merged_tokens.append(word.word)
                    entries.append({"start": word.start_sec, "end": word.end_sec, "word": word.word})
                    last_time = max(last_time, word.start_sec)
                merged_text = " ".join(token for token in merged_tokens if token).strip()

            write_json(transcript_json, {"text": merged_text, "entries": entries})
            transcript_txt.write_text(merged_text, encoding="utf-8")

            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("task not found during merge")
            task.transcript_path = str(transcript_json)
            await session.commit()

        for path in dirs["segments"].glob("segment_*.wav"):
            path.unlink(missing_ok=True)
        await self.bus.publish_event(
            user_id=user_id,
            task_id=str(task_id),
            event="phase",
            data={"phase": "merge_transcript", "status": "done"},
        )
        return True

    async def step_summarize_windows(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        dry_run: bool,
    ) -> bool:
        output = dirs["outputs"] / "window_summaries.json"
        if output.exists():
            return True
        if dry_run:
            return False

        transcript_json = dirs["outputs"] / "transcript.json"
        if not transcript_json.exists():
            raise RuntimeError("Missing transcript for summarization")
        transcript = json.loads(transcript_json.read_text(encoding="utf-8")).get("text", "")
        if not isinstance(transcript, str) or not transcript.strip():
            write_json(output, {"windows": []})
            return True

        segment_prompt = load_prompt(
            self.settings.prompts_dir,
            "segment_prompt.md",
            "Return JSON with keys: topic, bullets, action_items.",
        )
        chunks = chunk_text(transcript, window_tokens=2000, overlap_ratio=0.15)
        windows: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            async with self.heavy_slot:
                raw = await llama_chat_completion(
                    llama_url=self.settings.llama_url,
                    model=self.settings.llama_model,
                    system_prompt=segment_prompt,
                    user_prompt=f"Window {idx + 1}/{len(chunks)}\n\n{chunk}",
                )
            parsed = parse_json_response(raw)
            windows.append({"window_index": idx, "summary": parsed})
            await self.bus.publish_event(
                user_id=user_id,
                task_id=str(task_id),
                event="summary_progress",
                data={"current": idx + 1, "total": len(chunks)},
                throttle_key="summary_progress",
            )
        write_json(output, {"windows": windows})
        logger.info("window summaries generated: %s", len(windows))
        return True

    async def step_summarize_final(
        self,
        task_id: uuid.UUID,
        user_id: str,
        dirs: dict[str, Path],
        logger: logging.Logger,
        dry_run: bool,
    ) -> bool:
        summary_json = dirs["outputs"] / "summary.json"
        summary_md = dirs["outputs"] / "summary.md"
        if summary_json.exists() and summary_md.exists():
            return True
        if dry_run:
            return False

        windows_file = dirs["outputs"] / "window_summaries.json"
        if not windows_file.exists():
            raise RuntimeError("Missing window summaries")
        windows = json.loads(windows_file.read_text(encoding="utf-8")).get("windows", [])
        global_prompt = load_prompt(
            self.settings.prompts_dir,
            "global_prompt.md",
            "Produce JSON with executive_summary, key_points, risks, decisions.",
        )
        merged = json.dumps(windows, ensure_ascii=True)
        async with self.heavy_slot:
            raw = await llama_chat_completion(
                llama_url=self.settings.llama_url,
                model=self.settings.llama_model,
                system_prompt=global_prompt,
                user_prompt=merged,
            )
        parsed = parse_json_response(raw)
        write_json(summary_json, parsed)
        summary_md.write_text(self._summary_markdown(parsed), encoding="utf-8")
        logger.info("final summary generated")

        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                raise RuntimeError("task not found during final summary")
            task.summary_path = str(summary_json)
            await session.commit()

        await self._cleanup_media(dirs["media"])
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

    async def _task_language(self, task_id: uuid.UUID) -> str | None:
        async with self.session_factory() as session:
            repo = Repo(session)
            task = await repo.get_task_by_id(task_id)
            if task is None:
                return None
            value = task.options.get("language")
            return str(value) if value else None

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

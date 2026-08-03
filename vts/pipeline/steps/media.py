from __future__ import annotations

import asyncio
import shutil
import uuid
from typing import TYPE_CHECKING, Any

from vts.db.repo import Repo
from vts.services.downloader import download_video_and_audio
from vts.services.media import (
    build_segments,
    concat_to_audio_16k_mono,
    concat_video_stream_copy,
    detect_silence_points,
    export_segments,
    extract_audio_16k_mono,
    probe_duration,
    trim_initial_silence,
)
from vts.services.storage import write_json
from vts.pipeline.steps.base import Step, StepState

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


# Progress callbacks below run on a worker thread (asyncio.to_thread) and hand
# the publish back to the loop. Two footguns live in that handoff, both fixed
# here rather than repeated at each call site (vts-c58):
#
#  1. The event loop holds only WEAK references to tasks, so a bare
#     `asyncio.create_task(...)` with nothing keeping the result alive can be
#     garbage-collected mid-publish. Keeping a strong reference until the task
#     finishes is the documented remedy.
#  2. A `lambda` closing over the callback's arguments captures them by
#     reference. These callbacks fire repeatedly and rapidly, so the next call
#     could rebind the name before the scheduled lambda ran, publishing the
#     wrong payload. Passing the coroutine in as a default argument binds it at
#     schedule time instead.
_background_publishes: set[asyncio.Task] = set()


def _publish_soon(loop: asyncio.AbstractEventLoop, make_coro) -> None:
    """Schedule `make_coro()` on `loop` from a worker thread, safely."""

    def _schedule(make_coro=make_coro) -> None:
        task = asyncio.create_task(make_coro())
        _background_publishes.add(task)
        task.add_done_callback(_background_publishes.discard)

    loop.call_soon_threadsafe(_schedule)


class DownloadStep(Step):
    name = "download"
    lane = "network"

    def _done(self, ctx: "PipelineContext", st: StepState) -> bool:
        audio_only = ctx.task_flag(st.task_options, "audio_only", default=False)
        video_file = st.dirs["media"] / "video.mkv"
        audio_file = next(st.dirs["media"].glob("audio.original.*"), None)
        if audio_only and audio_file:
            return True
        if not audio_only and video_file.exists() and audio_file:
            return True
        return False

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        if self._done(ctx, st):
            return True
        # Media files may have been cleaned up after a completed run.
        # If transcript or audio segments already exist, download is not needed.
        transcript_json = st.dirs["outputs"] / "transcript.json"
        return transcript_json.exists() or any(st.dirs["segments"].glob("*.wav"))

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        audio_only = ctx.task_flag(st.task_options, "audio_only", default=False)
        video_file = st.dirs["media"] / "video.mkv"
        audio_file = next(st.dirs["media"].glob("audio.original.*"), None)
        if audio_only and audio_file:
            return True
        if not audio_only and video_file.exists() and audio_file:
            return True

        source_url = await ctx.task_url(st.task_id)

        # Uploaded file: already in place, skip yt-dlp download entirely.
        if source_url.startswith("file://"):
            audio_file = next(st.dirs["media"].glob("audio.original.*"), None)
            if audio_file:
                st.logger.info("skipping download — using uploaded file: %s", source_url)
                return True
            raise RuntimeError(f"Uploaded file not found in media dir: {st.dirs['media']}")

        user_uuid = uuid.UUID(st.user_id)
        preferred_youtube_client = await ctx.get_user_preferred_ytdlp_client(user_uuid)
        if preferred_youtube_client:
            st.logger.info("using saved yt-dlp youtube client for user: %s", preferred_youtube_client)
        loop = asyncio.get_running_loop()
        captured_title: list[str] = []

        def sync_progress(phase: str, payload: dict[str, Any]) -> None:
            if not captured_title:
                title = payload.get("media_title")
                if title and isinstance(title, str):
                    captured_title.append(title.strip())
            merged_data = {"phase": phase, **payload}
            _publish_soon(
                loop,
                lambda data=merged_data: ctx.bus.publish_event(
                    user_id=st.user_id,
                    task_id=str(st.task_id),
                    event="media_progress",
                    data=data,
                    throttle_key="media_progress",
                ),
            )

        def sync_phase(phase: str, status: str) -> None:
            _publish_soon(
                loop,
                lambda phase=phase, status=status: ctx.bus.publish_event(
                    user_id=st.user_id,
                    task_id=str(st.task_id),
                    event="phase",
                    data={"phase": phase, "status": status},
                ),
            )

        _, _, selected_youtube_client = await asyncio.to_thread(
            download_video_and_audio,
            source_url=source_url,
            media_dir=st.dirs["media"],
            progress_cb=sync_progress,
            phase_cb=sync_phase,
            logger=st.logger,
            audio_only=audio_only,
            preferred_youtube_client=preferred_youtube_client,
            ytdlp_cookies_file=ctx.settings.ytdlp_cookies_file,
            ytdlp_cookies_from_browser=ctx.settings.ytdlp_cookies_from_browser,
            ytdlp_youtube_player_client=ctx.settings.ytdlp_youtube_player_client,
            ytdlp_youtube_po_token=ctx.settings.ytdlp_youtube_po_token,
            ytdlp_verbose=ctx.settings.ytdlp_verbose,
        )
        if selected_youtube_client and selected_youtube_client != preferred_youtube_client:
            await ctx.set_user_preferred_ytdlp_client(user_uuid, selected_youtube_client)
            st.logger.info("saved yt-dlp youtube client for user: %s", selected_youtube_client)
        if captured_title:
            await ctx.save_task_source_title(st.task_id, captured_title[0])
        st.logger.info("download finished")
        return True


class ExtractAudioStep(Step):
    name = "extract_audio"
    lane = "ffmpeg"

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        output = st.dirs["media"] / "audio_16k.wav"
        trimmed = st.dirs["media"] / "audio_16k_trimmed.wav"
        # After trim step we remove audio_16k.wav, so resume from later stages
        # must treat the trimmed WAV as a valid completion marker too.
        if trimmed.exists():
            return True
        if output.exists():
            return True
        # Media files may have been cleaned up after a completed run.
        transcript_json = st.dirs["outputs"] / "transcript.json"
        return transcript_json.exists() or any(st.dirs["segments"].glob("*.wav"))

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        output = st.dirs["media"] / "audio_16k.wav"
        trimmed = st.dirs["media"] / "audio_16k_trimmed.wav"
        # After trim step we remove audio_16k.wav, so resume from later stages
        # must treat the trimmed WAV as a valid completion marker too.
        if trimmed.exists():
            return True
        if output.exists():
            return True

        log_path = st.dirs["logs"] / "task.log"
        source_files = (st.task_options or {}).get("source_files") or []

        if source_files:
            # A multi-file set (vts-vm0). Parts are named audio.original.NNN.*
            # where NNN is the concat order, so sorting by name IS the order.
            parts = sorted(
                p for p in st.dirs["media"].glob("audio.original.*")
                if not p.name.endswith(".probe.json") and not p.name.endswith(".part")
            )
            if len(parts) != len(source_files):
                raise RuntimeError(
                    f"Expected {len(source_files)} uploaded parts, found {len(parts)}"
                )

            work = st.dirs["media"] / "concat_work"
            durations = await asyncio.to_thread(
                concat_to_audio_16k_mono, parts, output, log_path, work
            )

            # Record real boundaries now that the durations are known.
            offset = 0.0
            updated = []
            for entry, duration in zip(source_files, durations):
                item = dict(entry)
                item["offset_sec"] = round(offset, 3)
                item["duration_sec"] = round(duration, 3)
                updated.append(item)
                offset += duration
            # JSON column: reassign, never mutate in place.
            options = dict(st.task_options or {})
            options["source_files"] = updated
            st.task_options = options

            if options.get("source_files_kind") == "video":
                # The player resolves media via _find_media_file, which prefers
                # video.mkv — without this a video set would play one part.
                await asyncio.to_thread(
                    concat_video_stream_copy, parts, st.dirs["media"] / "video.mkv", log_path, work
                )

            shutil.rmtree(work, ignore_errors=True)
            st.logger.info("audio extraction finished (multi-file set, %d parts)", len(parts))
            await ctx.persist_task_options(st.task_id, options)
            return True

        audio_file = next(st.dirs["media"].glob("audio.original.*"), None)
        if not audio_file:
            raise RuntimeError("Missing downloaded audio file")
        await asyncio.to_thread(
            extract_audio_16k_mono,
            audio_file,
            output,
            log_path,
        )
        st.logger.info("audio extraction finished")
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="phase",
            data={"phase": "extract_audio", "status": "done"},
        )
        return True


class TrimInitialSilenceStep(Step):
    name = "trim_initial_silence"
    lane = "ffmpeg"

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        output = st.dirs["media"] / "audio_16k_trimmed.wav"
        marker = st.dirs["outputs"] / "audio_preprocess.json"
        if output.exists() and marker.exists():
            return True
        # Media files may have been cleaned up after a completed run.
        transcript_json = st.dirs["outputs"] / "transcript.json"
        return transcript_json.exists() or any(st.dirs["segments"].glob("*.wav"))

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        source = st.dirs["media"] / "audio_16k.wav"
        output = st.dirs["media"] / "audio_16k_trimmed.wav"
        marker = st.dirs["outputs"] / "audio_preprocess.json"
        if output.exists() and marker.exists():
            return True
        if not source.exists():
            raise RuntimeError("Missing extracted WAV")

        trimmed_seconds = await asyncio.to_thread(
            trim_initial_silence,
            source,
            output,
            st.dirs["logs"] / "task.log",
            threshold_db=ctx.settings.trim_silence_threshold_db,
            min_duration_sec=ctx.settings.trim_silence_min_duration_sec,
            max_trim_seconds=ctx.settings.trim_silence_max_seconds,
        )
        payload = {
            "source": str(source),
            "output": str(output),
            "trimmed_seconds": round(trimmed_seconds, 3),
            "threshold_db": ctx.settings.trim_silence_threshold_db,
            "min_duration_sec": ctx.settings.trim_silence_min_duration_sec,
            "max_trim_seconds": ctx.settings.trim_silence_max_seconds,
        }
        write_json(marker, payload)
        source.unlink(missing_ok=True)
        st.logger.info("initial silence trim finished: trimmed=%.3fs", trimmed_seconds)
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="phase",
            data={"phase": "trim_initial_silence", "status": "done", "trimmed_seconds": round(trimmed_seconds, 3)},
        )
        return True


class SegmentAudioStep(Step):
    name = "segment_audio"
    lane = "ffmpeg"

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        manifest_path = st.dirs["outputs"] / "segments_manifest.json"
        if manifest_path.exists():
            return True
        # Transcript exists means segmentation output was already consumed.
        transcript_json = st.dirs["outputs"] / "transcript.json"
        return transcript_json.exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        manifest_path = st.dirs["outputs"] / "segments_manifest.json"
        if manifest_path.exists():
            return True

        audio_wav = ctx.transcribe_audio_path(st.dirs)
        if not audio_wav.exists():
            raise RuntimeError("Missing extracted WAV")

        duration = await asyncio.to_thread(probe_duration, audio_wav)
        silence_points = await asyncio.to_thread(
            detect_silence_points,
            audio_wav,
            st.dirs["logs"] / "task.log",
            ctx.settings.segment_search_window_seconds,
        )
        segments = build_segments(
            duration_sec=duration,
            target_seconds=ctx.settings.segment_target_seconds,
            search_window_seconds=ctx.settings.segment_search_window_seconds,
            overlap_seconds=ctx.settings.segment_overlap_seconds,
            silence_points=silence_points,
        )
        total_segments = len(segments)
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="segment_progress",
            data={"current": 0, "total": total_segments},
        )
        loop = asyncio.get_running_loop()

        def sync_segment_progress(current: int, total: int) -> None:
            _publish_soon(
                loop,
                lambda current=int(current), total=int(total): ctx.bus.publish_event(
                    user_id=st.user_id,
                    task_id=str(st.task_id),
                    event="segment_progress",
                    data={"current": current, "total": total},
                    throttle_key="segment_progress",
                ),
            )

        specs = await asyncio.to_thread(
            export_segments,
            audio_wav,
            segments,
            st.dirs["segments"],
            st.dirs["logs"] / "task.log",
            sync_segment_progress,
        )
        st.logger.info("segmentation finished with %s segments", len(specs))
        write_json(manifest_path, {"segments": specs})
        await ctx.bus.publish_event(
            user_id=st.user_id,
            task_id=str(st.task_id),
            event="phase",
            data={"phase": "segment_audio", "segments": len(specs)},
        )
        async with ctx.session_factory() as session:
            repo = Repo(session)
            await repo.clear_asr_for_task(st.task_id)
            for spec in specs:
                await repo.upsert_asr_segment_payload(
                    task_id=st.task_id,
                    segment_index=int(spec["segment_index"]),
                    start_sec=float(spec["start"]),
                    end_sec=float(spec["end"]),
                    text="",
                    raw_json={},
                )
            await session.commit()
        return True

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from vts.pipeline.steps.base import Step, StepState
from vts.services.media import run_ffmpeg
from vts.services.storage import write_json

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


class DiarizationCancelled(Exception):
    """The task was cancelled while its diarization was still running."""


def diarize_enabled(task_options: dict, default: bool) -> bool:
    """Per-task `diarize`, falling back to the configured default."""
    value = task_options.get("diarize")
    if value is None:
        return default
    return bool(value)


def select_preview_spans(
    segments: list[dict], speaker: str, *, count: int, clip_seconds: float, min_segment: float,
) -> list[dict]:
    """Pick up to `count` clip spans of `clip_seconds` for one speaker.

    Prefers spreading clips across distinct segments (longest first), cutting each
    from the segment's middle. When usable segments run out, takes additional
    non-overlapping clips from the longest one so a monologue still yields variety.
    """
    own = [s for s in segments if str(s["speaker"]) == speaker
           and (float(s["end"]) - float(s["start"])) >= min_segment]
    own.sort(key=lambda s: float(s["end"]) - float(s["start"]), reverse=True)

    def middle_clip(seg: dict, offset: float = 0.0) -> dict:
        start, end = float(seg["start"]), float(seg["end"])
        length = end - start
        clip = min(clip_seconds, length)
        mid = start + (length - clip) / 2 + offset
        mid = max(start, min(mid, end - clip))
        return {"start": round(mid, 3), "end": round(mid + clip, 3)}

    spans: list[dict] = []
    for seg in own:
        if len(spans) >= count:
            break
        spans.append(middle_clip(seg))
    # Scarce case: refill from the longest segment with additional non-overlapping
    # clips, tiled sequentially from its start (skipping past whatever spans are
    # already placed) so a monologue still yields several distinct-sounding clips.
    if len(spans) < count and own:
        longest = own[0]
        start, end = float(longest["start"]), float(longest["end"])
        cursor = start
        while len(spans) < count and cursor + clip_seconds <= end:
            candidate = {"start": round(cursor, 3), "end": round(cursor + clip_seconds, 3)}
            if not any(candidate["start"] < s["end"] and s["start"] < candidate["end"] for s in spans):
                spans.append(candidate)
            cursor += clip_seconds
    return spans[:count]


def _cut_wav(src: Path, dst: Path, start: float, end: float) -> None:
    """Cut a preview clip from `src` into `dst` via ffmpeg.

    Re-encodes (not `-c copy`): a stream copy at an arbitrary cut point on wav
    PCM data is not guaranteed to land on a clean sample boundary.
    """
    duration = max(0.0, end - start)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-ar",
        "16000",
        "-ac",
        "1",
        str(dst),
    ]
    run_ffmpeg(cmd)


class DiarizeStep(Step):
    name = "diarize"
    # Its own lane, not `gpu`: the sidecar runs pyannote on the CPU, so it
    # contends with other diarizations rather than with Whisper. Without a lane
    # the worker's four in-flight tasks could start four of them at once, each
    # wanting 8 threads — they would finish slower than if they had queued.
    lane = "diarize"

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return (st.dirs["outputs"] / "diarization.json").exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        default = bool(getattr(ctx.settings, "diarization_enabled_default", False))
        if not diarize_enabled(st.task_options, default):
            st.logger.info("diarization skipped: disabled for this task")
            return True

        output = st.dirs["outputs"] / "diarization.json"
        if output.exists():
            return True

        # The whole audio, never the per-chunk WAVs: chunks are cut by duration
        # for parallel transcription, so the same person in two chunks would get
        # two different speaker tags.
        audio_path = ctx.transcribe_audio_path(st.dirs)
        if not audio_path.exists():
            raise RuntimeError(f"Missing audio for diarization: {audio_path}")

        async def report(step: str, completed: int, total: int) -> None:
            # Cancellation is checked here because this is the only place that
            # runs during diarization: the processor only tests between steps,
            # so a task deleted mid-run used to leave the sidecar grinding for
            # the rest of its 25 minutes with nobody left to want the answer.
            if await ctx.bus.is_cancel_requested(st.task_id):
                await ctx.diarization.cancel(str(st.task_id))
                raise DiarizationCancelled
            await ctx.bus.publish_event(
                user_id=st.user_id,
                task_id=str(st.task_id),
                event="diarize_progress",
                data={"step": step, "completed": completed, "total": total},
                throttle_key="diarize_progress",
            )

        # The task id doubles as the job id: it is already persistent, so a
        # worker that restarts mid-run re-attaches to the job still running in
        # the sidecar instead of paying for the whole diarization twice.
        payload = await ctx.diarization.diarize(
            audio_path=audio_path,
            job_id=str(st.task_id),
            on_progress=report,
        )

        # We sent audio and got no speakers back. This is NOT what a monologue
        # looks like — a real single-speaker result is one segment spanning the
        # audio, never zero. So this means the sidecar failed or returned
        # something unparseable, and normalize_output degraded it to empty.
        # Writing the artifact anyway would render flat text: a broken sidecar
        # would be indistinguishable from a genuine monologue — wrong, but not
        # obviously wrong, which is the worst failure shape to ship.
        if not payload.get("segments"):
            raise RuntimeError(
                "Diarization returned no speaker segments; refusing to write an "
                "empty artifact that would silently render as a monologue"
            )

        write_json(output, payload)
        st.logger.info("diarization finished: speakers=%s", payload.get("num_speakers"))

        # Cut representative preview clips per speaker, so a human can later
        # listen to who's who when naming voices (vts-80i). A separate artifact
        # from diarization.json: already_done above only keys on the latter, so
        # this never affects idempotency of the diarize step itself.
        previews: dict[str, list[dict]] = {}
        for label in {s["speaker"] for s in payload["segments"]}:
            spans = select_preview_spans(
                payload["segments"], str(label),
                count=ctx.settings.speaker_preview_count,
                clip_seconds=ctx.settings.speaker_preview_seconds,
                min_segment=ctx.settings.speaker_preview_min_segment,
            )
            clips = []
            for i, span in enumerate(spans):
                clip_path = st.dirs["outputs"] / f"preview_{label}_{i}.wav"
                await asyncio.to_thread(_cut_wav, audio_path, clip_path, span["start"], span["end"])
                clips.append({"path": str(clip_path), "start": span["start"], "end": span["end"]})
            previews[str(label)] = clips
        write_json(st.dirs["outputs"] / "speaker_previews.json", previews)

        return True

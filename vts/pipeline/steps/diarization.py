from __future__ import annotations

from typing import TYPE_CHECKING

from vts.pipeline.steps.base import Step, StepState
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
        return True

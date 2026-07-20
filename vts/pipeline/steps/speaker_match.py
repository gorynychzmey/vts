from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from vts.db.repo import Repo
from vts.pipeline.steps.base import Step, StepState
from vts.pipeline.steps.diarization import diarize_enabled
from vts.services.diarization.merge import auto_noise_labels, speaker_seconds, speaker_shares
from vts.services.speaker_registry import MatchOutcome, bucket
from vts.services.storage import write_json

if TYPE_CHECKING:
    from vts.pipeline.context import PipelineContext


def decide_pause(matches: dict, no_stop: bool) -> bool:
    """Pause iff a human is needed: any speaker not auto-resolved and stops allowed.

    Noise speakers are skipped: a cluster flagged noise (echo, or no transcript
    entries at all) has nothing for a human to resolve, so it must not hold the
    task at awaiting_input on its own.
    """
    if no_stop:
        return False
    return any(
        m["outcome"] != MatchOutcome.auto and not m.get("noise", False)
        for m in matches.values()
    )


def labels_without_entries(
    diar_labels: set[str], entries: list[dict[str, object]]
) -> set[str]:
    """Diarization labels that produced no transcript entry at all.

    Several voices can land in one short interval where Whisper transcribes
    nothing, leaving a diarization cluster with real speaking time but zero words
    in the transcript. There is nothing to attribute to such a label, so it is
    noise for resolution purposes. Entries with a None speaker (undiarized audio)
    contribute to no label and are ignored.
    """
    spoke = {str(e.get("speaker")) for e in entries if e.get("speaker") is not None}
    return {label for label in diar_labels if label not in spoke}


class MatchSpeakersStep(Step):
    """Matches each diarized speaker cluster against the user's voice registry.

    Runs after diarize: reads the cluster embeddings diarization.json carries,
    ranks each against the registry via repo.nearest_speakers, and buckets the
    nearest distance into auto/grey/miss. Writes speaker_matches.json, then
    pauses the task into awaiting_input (TaskAwaitingInput) when any speaker
    didn't auto-resolve, unless the task opted out via speaker_no_manual_stop.
    """

    name = "match_speakers"
    lane = None

    async def already_done(self, ctx: "PipelineContext", st: StepState) -> bool:
        return (st.dirs["outputs"] / "speaker_matches.json").exists()

    async def run(self, ctx: "PipelineContext", st: StepState) -> bool:
        default = bool(getattr(ctx.settings, "diarization_enabled_default", False))
        if not diarize_enabled(st.task_options, default):
            return True

        diar_path = st.dirs["outputs"] / "diarization.json"
        if not diar_path.exists():
            return True  # nothing to match

        diar = json.loads(diar_path.read_text(encoding="utf-8"))
        model = diar.get("embedding_model", "")
        embeddings = diar.get("embeddings", {})
        segments = diar.get("segments", []) or []

        shares = speaker_shares(segments)
        seconds = speaker_seconds(segments)
        noise_labels = auto_noise_labels(
            shares,
            embeddings,
            min_share=float(getattr(ctx.settings, "diarization_min_speaker_share", 0.05)),
            max_distance=float(getattr(ctx.settings, "diarization_noise_max_distance", 0.25)),
        )
        # merge_transcript ran before this step, so transcript.json exists and its
        # entries already carry the final speaker attribution. A diarization label
        # with real speaking time but no entry at all (overlapping voices Whisper
        # never transcribed) is noise for resolution — there is nothing to bind.
        # This is orthogonal to auto_noise_labels' echo test, so union the two.
        diar_labels = {str(segment["speaker"]) for segment in segments if segment.get("speaker")}
        transcript_path = st.dirs["outputs"] / "transcript.json"
        if transcript_path.exists():
            try:
                payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                payload = {}
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(entries, list):
                noise_labels = noise_labels | labels_without_entries(diar_labels, entries)

        auto = ctx.settings.speaker_match_max_distance_auto
        cand = ctx.settings.speaker_match_max_distance_candidate
        matches: dict[str, dict] = {}
        async with ctx.session_factory() as session:
            repo = Repo(session)
            # limit is a pathology guard (hundreds/thousands of speakers), NOT
            # a UX top-N: the resolution dialog needs ALL of the user's
            # candidates sorted by distance, so a real match is never hidden
            # behind a cutoff. speaker_match_candidates_cap defaults far above
            # any expected personal registry, so "all candidates" holds in
            # practice.
            cap = ctx.settings.speaker_match_candidates_cap
            for label, vector in embeddings.items():
                ranked = await repo.nearest_speakers(uuid.UUID(st.user_id), vector, model, limit=cap)
                nearest = ranked[0] if ranked else None
                dist = nearest[1] if nearest else None
                outcome = bucket(dist, auto=auto, candidate=cand)
                matches[label] = {
                    "outcome": str(outcome),
                    "speaker_id": str(nearest[0].id) if (nearest and outcome == MatchOutcome.auto) else None,
                    "distance": dist,
                    "share": shares.get(label, 0.0),
                    "seconds": seconds.get(label, 0.0),
                    "noise": label in noise_labels,
                    "candidates": [
                        {"speaker_id": str(sp.id), "name": sp.name, "distance": d}
                        for sp, d in ranked
                    ],
                }

        write_json(st.dirs["outputs"] / "speaker_matches.json", matches)

        no_stop = ctx.task_flag(st.task_options, "speaker_no_manual_stop", default=False)
        if decide_pause(matches, no_stop):
            # Imported lazily: vts.pipeline.processor imports the step registry
            # at module load time (to resolve DAG steps), and this module is
            # registered there — a top-level import back to processor would
            # be circular. context.py hits the same constraint for TaskPaused.
            from vts.pipeline.processor import TaskAwaitingInput

            raise TaskAwaitingInput("match_speakers")
        return True

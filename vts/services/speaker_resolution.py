"""Saving a task's voice resolutions (who each SPEAKER_NN is).

One implementation for every caller: the web UI's speaker dialog
(`POST /api/tasks/{task_id}/speakers`) and the MCP Apps speaker view
(vts-vtma.6). The sequence carries hard-won fixes — the task row lock against
double clicks (prod 2026-10-11), the rollback of a fragment an earlier save of
the same label added (vts-3ij7), one commit for the registry writes so a
failure leaves nothing half-written, the transcript re-render and re-index in
the same transaction — so it must not exist twice.

Errors are raised as HTTPException: both callers already turn those into
their own error shape (an HTTP status, an MCP tool error).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.serialization import can_resolve_speakers_task
from vts.core.config import Settings
from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.pipeline.rerender import rerender_transcript
from vts.pipeline.steps.transcription import effective_language
from vts.services.indexing import reindex_task
from vts.services.redis_bus import RedisBus


async def resolve_task_speakers(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    resolutions: Iterable[Any],
    continue_task: bool,
    redis: Redis,
    settings: Settings,
    diarization: Any,
) -> dict[str, str]:
    """Apply `resolutions` (VoiceResolution-shaped objects) to the task's
    voices and return {speaker_label: "resolved"}."""
    repo = Repo(session)
    # Locked, so two saves of the SAME task run one after the other. Everything
    # below is read-then-write: find the prior decision's fragment and delete
    # it, then add a new one; upsert the recording row. Interleaved, both saves
    # read the same "before" and both write — prod 2026-10-11, a save that took
    # ~11 s with no feedback was clicked four times, and every voice of that
    # task ended up with 3–4 identical fragments. With an empty recording slot
    # the same overlap fails outright on uq_recordings_source_task. The lock is
    # released by the first commit below, by which point the fragments are
    # written, so the waiting save sees them and replaces rather than adds.
    task = await repo.get_task_for_user(user_id, task_id, lock=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not can_resolve_speakers_task(task):
        raise HTTPException(status_code=409, detail="cannot_resolve_speakers")

    artifact_dir = Path(task.artifact_dir)
    diar_path = artifact_dir / "outputs" / "diarization.json"
    embedding_model = ""
    if diar_path.exists():
        diar = json.loads(diar_path.read_text(encoding="utf-8"))
        embedding_model = diar.get("embedding_model", "") or ""

    previews_path = artifact_dir / "outputs" / "speaker_previews.json"
    previews: dict[str, list[dict]] = {}
    if previews_path.exists():
        previews = json.loads(previews_path.read_text(encoding="utf-8"))

    # Everything below runs on the ONE request-scoped `session` and is
    # committed exactly once at the end (or not at all, on error) — a
    # partial write here would leave the registry with a speaker missing
    # its fragment, or a fragment attributed to the wrong voice.
    results: dict[str, str] = {}
    for res in resolutions:
        speaker_id: uuid.UUID | None = None

        if res.action == "bind_new":
            if not res.new_name or not res.new_name.strip():
                raise HTTPException(status_code=422, detail="new_name is required for bind_new")
            sp = await repo.create_speaker(user_id, res.new_name.strip())
            speaker_id = sp.id
        elif res.action == "bind_existing":
            if not res.speaker_id:
                raise HTTPException(status_code=422, detail="speaker_id is required for bind_existing")
            sp = await repo.get_speaker(user_id, uuid.UUID(res.speaker_id))
            if sp is None:
                raise HTTPException(status_code=404, detail=f"Speaker not found: {res.speaker_id}")
            speaker_id = sp.id
        elif res.action == "accept_auto":
            if not res.speaker_id:
                raise HTTPException(status_code=422, detail="speaker_id is required for accept_auto")
            sp = await repo.get_speaker(user_id, uuid.UUID(res.speaker_id))
            if sp is None:
                raise HTTPException(status_code=404, detail=f"Speaker not found: {res.speaker_id}")
            speaker_id = sp.id
        elif res.action == "leave_anonymous":
            speaker_id = None
        else:
            raise HTTPException(status_code=422, detail=f"Unknown action: {res.action}")

        voice_sample_id: uuid.UUID | None = (
            uuid.UUID(res.voice_sample_id) if res.voice_sample_id else None
        )

        # Rollback: if this label was previously resolved (in an earlier
        # save of this same awaiting_input dialog) and that decision added a
        # fragment, drop it before this save adds its own. Only ever touches
        # a fragment this same task added (source_task_id == task_id is
        # implicit: record_decision below always writes this task_id as
        # source_task_id, so any voice_sample_id on a decision scoped to
        # this task_id was added by this task). Fragments predating this
        # task are never looked at here.
        #
        # Deliberately NOT conditioned on the speaker having changed
        # (vts-3ij7). The SPA resubmits the whole resolutions set, so
        # re-saving the dialog after editing some OTHER label replays this
        # one unchanged — same speaker, add_fragment still true. Rolling
        # back only on a rebind left the previous sample in place and added
        # a second one from the same clip: the speaker accumulates a
        # duplicate fragment per re-save, and the older one is orphaned
        # (no decision row references it any more, so a later rebind cannot
        # clean it up either). Both branches want the prior sample gone: on
        # a rebind it is attributed to the wrong voice, on a re-save it is
        # about to be superseded by an identical one.
        prior = await repo.find_prior_decision_sample(user_id, task_id, res.speaker_label)
        if prior is not None:
            _prior_speaker_id, prior_voice_sample_id = prior
            # Never delete a sample this save is going to keep referencing:
            # a client that echoes back the voice_sample_id from a previous
            # save (rather than asking for a new fragment) is pointing at a
            # row that must survive.
            if prior_voice_sample_id is not None and prior_voice_sample_id != voice_sample_id:
                await repo.delete_voice_sample(user_id, prior_voice_sample_id)

        if res.add_fragment and speaker_id is not None:
            if not embedding_model:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "cannot add voice fragment: task has no embedding model "
                        "(diarization.json missing or incomplete)"
                    ),
                )
            clips = previews.get(res.speaker_label) or []
            if not clips:
                raise HTTPException(
                    status_code=422,
                    detail=f"No preview fragment available for speaker_label {res.speaker_label!r}",
                )
            clip_path = Path(clips[0]["path"])
            if not clip_path.exists():
                raise HTTPException(
                    status_code=422,
                    detail=f"Preview fragment file missing: {clip_path}",
                )
            embedding = await diarization.embed(clip_path)
            duration_sec = float(clips[0]["end"]) - float(clips[0]["start"])
            sample = await repo.add_voice_sample(
                speaker_id=speaker_id,
                embedding=embedding,
                embedding_model=embedding_model,
                audio=clip_path.read_bytes(),
                audio_format="wav",
                duration_sec=duration_sec,
                source_task_id=task_id,
            )
            voice_sample_id = sample.id

        await repo.record_decision(
            user_id=user_id,
            source_task_id=task_id,
            speaker_label=res.speaker_label,
            speaker_id=speaker_id,
            voice_sample_id=voice_sample_id,
            distance=res.distance,
            embedding_model=embedding_model,
            outcome=res.outcome,
            is_noise=res.is_noise,
        )
        results[res.speaker_label] = "resolved"

    language = effective_language(
        task.options if isinstance(task.options, dict) else {},
        {"outputs": Path(task.artifact_dir) / "outputs"},
    )
    await rerender_transcript(task, session, language=language)

    # The transcript just changed, so the corpus index describes text that is no
    # longer there (vts-twe7). Re-indexing here rather than on the event keeps
    # it in the same transaction as the rename — the alternative is a window
    # where a search cites a speaker name the user has already corrected.
    await reindex_task(session, task, settings)
    await session.commit()

    bus = RedisBus(redis, settings)
    # Universal "transcript is whole again" signal (vts-at8): resolve/save
    # re-rendered the transcript with new speaker names / noise flags, so
    # the /player page and any other SPA tab re-fetch it live. Mirrors the
    # event MergeTranscriptStep fires on first assembly.
    await bus.publish_event(
        user_id=str(user_id),
        task_id=str(task_id),
        event="transcript_updated",
        data={"task_id": str(task_id)},
    )
    if continue_task:
        # Symmetric with /api/tasks/resume (vts-80i): clear any stale
        # pause request before requeuing, so a leftover pause flag can't
        # survive a resume through this path either.
        await bus.clear_pause_request(task_id)
        await repo.set_task_status(task, TaskStatus.queued)

    await session.commit()

    if continue_task:
        await bus.notify_queued()

    return results


async def speaker_matches_view(
    repo: Repo, user_id: uuid.UUID, task: Any
) -> dict[str, Any] | None:
    """The task's speaker_matches.json as the speaker dialogs show it, or None
    when there is none.

    Shared by GET /api/tasks/{task_id}/speaker-matches and the MCP speaker
    view, so both show the same decisions, display labels and current names.
    """
    from vts.api._helpers.serialization import _speaker_ordering_entries

    outputs = Path(task.artifact_dir) / "outputs"
    path = outputs / "speaker_matches.json"
    if not path.exists():
        return None
    try:
        matches = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(matches, dict):
        return None

    # Enrich each match with (a) the operator's LATEST saved decision, so a
    # reopened dialog shows the real binding rather than the stale auto-match
    # (bug #1), and (b) a display_label numbered exactly like the transcript
    # ("Голос N" by first appearance over the FULL diarization, incl. noise),
    # so the dialog and the transcript agree (bug #2). Both are additive —
    # existing fields are untouched.
    from vts.services.diarization.merge import label_map, speaker_label_word

    decisions = await repo.decisions_for_task(user_id, task.id)

    # Display labels: number by first appearance across the diarization
    # segments (every speaker, including noise), so a speaker excluded from
    # the transcript text still has a stable "Голос N". Fall back to the
    # transcript entries, then to the matches keys, if diarization.json is
    # unreadable.
    language = effective_language(
        task.options if isinstance(task.options, dict) else {},
        {"outputs": outputs},
    )
    ordering_entries = _speaker_ordering_entries(outputs, matches)
    display = label_map(ordering_entries, speaker_label_word(language))

    # Candidate names were frozen into speaker_matches.json at match time, so
    # a person renamed since then would render under the stale name. Reconcile
    # each candidate against the live registry (as decided_name already is),
    # so the dialog always shows the current name. A candidate whose person
    # was deleted is dropped from the live map and keeps its stored name.
    live_names = {
        str(speaker.id): speaker.name
        for speaker in await repo.list_speakers(user_id)
    }

    for label, entry in matches.items():
        if not isinstance(entry, dict):
            continue
        for candidate in entry.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            current = live_names.get(str(candidate.get("speaker_id")))
            if current is not None:
                candidate["name"] = current
        decision = decisions.get(label)
        entry["decided_speaker_id"] = decision["speaker_id"] if decision else None
        entry["decided_name"] = decision["name"] if decision else None
        entry["decided_is_noise"] = decision["is_noise"] if decision else None
        entry["display_label"] = display.get(label, label)

    return matches


def preview_clip_path(artifact_dir: str | None, speaker_label: str, index: int) -> Path | None:
    """The `index`-th preview clip of a voice, or None.

    SECURITY: the path comes out of speaker_previews.json, so it is checked to
    resolve inside this artifact directory's outputs before anything serves
    it — a defence against a tampered json or traversal through the label or
    index. Shared by the task route (web dialog) and the signed recording
    route (MCP speaker view).
    """
    if not artifact_dir:
        return None
    outputs_dir = Path(artifact_dir) / "outputs"
    previews_path = outputs_dir / "speaker_previews.json"
    try:
        previews = json.loads(previews_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(previews, dict):
        return None
    clips = previews.get(speaker_label)
    if not isinstance(clips, list) or index < 0 or index >= len(clips):
        return None
    raw = clips[index].get("path") if isinstance(clips[index], dict) else None
    if not raw:
        return None
    resolved = Path(raw).resolve()
    if not resolved.is_relative_to(outputs_dir.resolve()) or not resolved.is_file():
        return None
    return resolved


def preview_counts(artifact_dir: str | None) -> dict[str, int]:
    """How many preview clips each voice has, for a view to offer them."""
    if not artifact_dir:
        return {}
    try:
        previews = json.loads(
            (Path(artifact_dir) / "outputs" / "speaker_previews.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(previews, dict):
        return {}
    return {str(k): len(v) for k, v in previews.items() if isinstance(v, list)}


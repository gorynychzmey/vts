"""Speaker registry and per-task voice resolution (vts-80i).

Two surfaces that share the registry model:

* `/api/speakers/...` — the user's people, their stored voice fragments, and
  the merge/move operations between them.
* `/api/tasks/{task_id}/speaker-matches|speaker-previews|speakers` — matching
  a task's diarized voices against that registry, and committing the result.

Split out of `vts.api.main.create_app()` — see docs/plans/main-py-split.md.
Handler bodies are unchanged.

Route order matters here and is load-bearing: `/api/speakers/samples/{sample_id}/audio`
must keep matching its own handler rather than being swallowed by
`/api/speakers/{speaker_id}`. Both live in this one router, so the order below
is the order FastAPI sees — do not reorder them across a split.

Routers carry no `tags=`: `_install_custom_openapi()` in `vts.api.main` derives
the OpenAPI tag from the URL prefix, and an explicit tag would override it.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api._helpers.serialization import _speaker_ordering_entries, can_resolve_speakers_task
from vts.api.deps import (
    get_current_user,
    get_diarization_backend_dep,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    BatchResultOut,
    MergeSpeakersRequest,
    MoveCandidateOut,
    MoveVoiceSampleRequest,
    SpeakerCreateRequest,
    SpeakerOut,
    SpeakerUpdateRequest,
    VoiceResolutionRequest,
    VoiceSampleOut,
)
from vts.core.config import Settings, get_settings
from vts.db.models import TaskStatus
from vts.db.repo import Repo
from vts.pipeline.rerender import rerender_transcript
from vts.pipeline.steps.transcription import effective_language
from vts.services.auth import AuthenticatedUser
from vts.services.redis_bus import RedisBus

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/speakers", response_model=list[SpeakerOut])
async def list_speakers_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[SpeakerOut]:
    repo = Repo(session)
    speakers = await repo.list_speakers(uuid.UUID(user.id))
    out: list[SpeakerOut] = []
    for sp in speakers:
        samples = await repo.list_voice_samples(sp.id)
        out.append(SpeakerOut(id=str(sp.id), name=sp.name, sample_count=len(samples)))
    return out


@router.post("/api/speakers", response_model=SpeakerOut)
async def create_speaker_endpoint(
    payload: SpeakerCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> SpeakerOut:
    repo = Repo(session)
    sp = await repo.create_speaker(uuid.UUID(user.id), payload.name.strip())
    await session.commit()
    return SpeakerOut(id=str(sp.id), name=sp.name, sample_count=0)


@router.patch("/api/speakers/{speaker_id}", response_model=SpeakerOut)
async def rename_speaker_endpoint(
    speaker_id: uuid.UUID,
    payload: SpeakerUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> SpeakerOut:
    repo = Repo(session)
    sp = await repo.rename_speaker(uuid.UUID(user.id), speaker_id, payload.name.strip())
    if sp is None:
        raise HTTPException(status_code=404, detail="Speaker not found")
    await session.commit()
    samples = await repo.list_voice_samples(sp.id)
    return SpeakerOut(id=str(sp.id), name=sp.name, sample_count=len(samples))


@router.delete("/api/speakers/{speaker_id}", status_code=204)
async def delete_speaker_endpoint(
    speaker_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    ok = await repo.delete_speaker(uuid.UUID(user.id), speaker_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Speaker not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/api/speakers/{speaker_id}/samples", response_model=list[VoiceSampleOut])
async def list_voice_samples_endpoint(
    speaker_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[VoiceSampleOut]:
    repo = Repo(session)
    sp = await repo.get_speaker(uuid.UUID(user.id), speaker_id)
    if sp is None:
        raise HTTPException(status_code=404, detail="Speaker not found")
    samples = await repo.list_voice_samples(speaker_id)
    return [
        VoiceSampleOut(
            id=str(s.id),
            duration_sec=s.duration_sec,
            source_task_id=str(s.source_task_id) if s.source_task_id else None,
            created_at=s.created_at,
        )
        for s in samples
    ]


@router.delete("/api/speakers/{speaker_id}/samples/{sample_id}", status_code=204)
async def delete_voice_sample_endpoint(
    speaker_id: uuid.UUID,
    sample_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    sample = await repo.get_voice_sample(uuid.UUID(user.id), sample_id)
    if sample is None or sample.speaker_id != speaker_id:
        raise HTTPException(status_code=404, detail="Voice sample not found")
    ok = await repo.delete_voice_sample(uuid.UUID(user.id), sample_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Voice sample not found")
    await session.commit()
    return Response(status_code=204)


@router.post(
    "/api/speakers/{speaker_id}/samples/{sample_id}/move",
    response_model=VoiceSampleOut,
)
async def move_voice_sample_endpoint(
    speaker_id: uuid.UUID,
    sample_id: uuid.UUID,
    payload: MoveVoiceSampleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> VoiceSampleOut:
    repo = Repo(session)
    # the {speaker_id} segment must actually own {sample_id} — same guard as delete
    sample = await repo.get_voice_sample(uuid.UUID(user.id), sample_id)
    if sample is None or sample.speaker_id != speaker_id:
        raise HTTPException(status_code=404, detail="Voice sample not found")
    moved = await repo.move_voice_sample(
        uuid.UUID(user.id), sample_id, payload.target_speaker_id
    )
    if moved is None:
        raise HTTPException(status_code=404, detail="Target speaker not found")
    await session.commit()
    return VoiceSampleOut(
        id=str(moved.id),
        duration_sec=moved.duration_sec,
        source_task_id=str(moved.source_task_id) if moved.source_task_id else None,
        created_at=moved.created_at,
    )


@router.get(
    "/api/speakers/{speaker_id}/samples/{sample_id}/move-candidates",
    response_model=list[MoveCandidateOut],
)
async def move_candidates_endpoint(
    speaker_id: uuid.UUID,
    sample_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[MoveCandidateOut]:
    repo = Repo(session)
    sample = await repo.get_voice_sample(uuid.UUID(user.id), sample_id)
    if sample is None or sample.speaker_id != speaker_id:
        raise HTTPException(status_code=404, detail="Voice sample not found")
    # Safety bound, not a UX top-N — same cap the match step uses so the
    # dialog and the pipeline agree on how many candidates are considered.
    cap = int(getattr(get_settings(), "speaker_match_candidates_cap", 100))
    ranked = await repo.move_candidates_for_sample(
        uuid.UUID(user.id), sample_id, limit=cap
    )
    out: list[MoveCandidateOut] = []
    for speaker, distance in ranked:
        samples = await repo.list_voice_samples(speaker.id)
        out.append(
            MoveCandidateOut(
                id=str(speaker.id),
                name=speaker.name,
                sample_count=len(samples),
                distance=distance,
            )
        )
    return out


@router.post("/api/speakers/{source_id}/merge", status_code=204)
async def merge_speakers_endpoint(
    source_id: uuid.UUID,
    payload: MergeSpeakersRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    if source_id == payload.target_id:
        raise HTTPException(status_code=409, detail="Cannot merge a speaker into itself")
    repo = Repo(session)
    ok = await repo.merge_speakers(uuid.UUID(user.id), source_id, payload.target_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Speaker not found")
    await session.commit()
    return Response(status_code=204)


@router.get("/api/speakers/samples/{sample_id}/audio")
async def get_sample_audio(
    sample_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    loaded = await repo.load_sample_audio(uuid.UUID(user.id), sample_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    audio, fmt = loaded
    return Response(content=audio, media_type=f"audio/{fmt}")


@router.get("/api/tasks/{task_id}/speaker-matches")
async def get_speaker_matches(
    task_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> Response:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    outputs = Path(task.artifact_dir) / "outputs"
    path = outputs / "speaker_matches.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Speaker matches not found")
    try:
        matches = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Speaker matches not found")
    if not isinstance(matches, dict):
        raise HTTPException(status_code=404, detail="Speaker matches not found")

    # Enrich each match with (a) the operator's LATEST saved decision, so a
    # reopened dialog shows the real binding rather than the stale auto-match
    # (bug #1), and (b) a display_label numbered exactly like the transcript
    # ("Голос N" by first appearance over the FULL diarization, incl. noise),
    # so the dialog and the transcript agree (bug #2). Both are additive —
    # existing fields are untouched.
    from vts.services.diarization.merge import label_map, speaker_label_word

    decisions = await repo.decisions_for_task(uuid.UUID(user.id), task_id)

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
        for speaker in await repo.list_speakers(uuid.UUID(user.id))
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

    return JSONResponse(matches)


@router.get("/api/tasks/{task_id}/speaker-previews/{speaker_label}/{index}/audio")
async def get_speaker_preview_audio(
    task_id: uuid.UUID,
    speaker_label: str,
    index: int,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> FileResponse:
    repo = Repo(session)
    task = await repo.get_task_for_user(uuid.UUID(user.id), task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    outputs_dir = Path(task.artifact_dir) / "outputs"
    previews_path = outputs_dir / "speaker_previews.json"
    if not previews_path.exists():
        raise HTTPException(status_code=404, detail="Speaker previews not found")

    try:
        previews = json.loads(previews_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Speaker previews not found")
    if not isinstance(previews, dict):
        raise HTTPException(status_code=404, detail="Speaker previews not found")

    clips = previews.get(speaker_label)
    if not isinstance(clips, list) or index < 0 or index >= len(clips):
        raise HTTPException(status_code=404, detail="Preview clip not found")

    clip_path_str = clips[index].get("path") if isinstance(clips[index], dict) else None
    if not clip_path_str:
        raise HTTPException(status_code=404, detail="Preview clip not found")

    # SECURITY: never trust the resolved path just because it came out of
    # the json. Confirm it actually resolves to somewhere inside this
    # task's outputs dir before serving it — defense against a tampered
    # speaker_previews.json or path-traversal via speaker_label/index.
    resolved_outputs_dir = outputs_dir.resolve()
    resolved_clip_path = Path(clip_path_str).resolve()
    if not resolved_clip_path.is_relative_to(resolved_outputs_dir):
        raise HTTPException(status_code=404, detail="Preview clip not found")
    if not resolved_clip_path.is_file():
        raise HTTPException(status_code=404, detail="Preview clip not found")

    return FileResponse(path=str(resolved_clip_path), media_type="audio/wav")


@router.post("/api/tasks/{task_id}/speakers", response_model=BatchResultOut)
async def resolve_task_speakers(
    task_id: uuid.UUID,
    payload: VoiceResolutionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
    diarization=Depends(get_diarization_backend_dep),
) -> BatchResultOut:
    repo = Repo(session)
    user_id = uuid.UUID(user.id)
    task = await repo.get_task_for_user(user_id, task_id)
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
    for res in payload.resolutions:
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
    if payload.continue_task:
        # Symmetric with /api/tasks/resume (vts-80i): clear any stale
        # pause request before requeuing, so a leftover pause flag can't
        # survive a resume through this path either.
        await bus.clear_pause_request(task_id)
        await repo.set_task_status(task, TaskStatus.queued)

    await session.commit()

    if payload.continue_task:
        await bus.notify_queued()

    return BatchResultOut(results=results)

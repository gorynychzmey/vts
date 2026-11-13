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
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser

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
    from vts.services.speaker_resolution import speaker_matches_view

    matches = await speaker_matches_view(repo, uuid.UUID(user.id), task)
    if matches is None:
        raise HTTPException(status_code=404, detail="Speaker matches not found")
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

    from vts.services.speaker_resolution import preview_clip_path

    clip = preview_clip_path(task.artifact_dir, speaker_label, index)
    if clip is None:
        raise HTTPException(status_code=404, detail="Preview clip not found")
    return FileResponse(path=str(clip), media_type="audio/wav")


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
    from vts.services.speaker_resolution import resolve_task_speakers as _resolve

    results = await _resolve(
        session,
        user_id=uuid.UUID(user.id),
        task_id=task_id,
        resolutions=payload.resolutions,
        continue_task=payload.continue_task,
        redis=redis,
        settings=settings,
        diarization=diarization,
    )
    return BatchResultOut(results=results)

"""Resumable chunked-upload endpoints (vts-b8j).

Split out of `vts.api.main.create_app()` unchanged — see
docs/plans/main-py-split.md. The handlers already took `settings` and the
other request-scoped objects via `Depends`, so nothing here relied on the
old enclosing closure; only `app` became `router`.

`_ALLOWED_UPLOAD_SUFFIXES` and the task-creation helpers still live in
`vts.api.main`; they are reached through the `_main()` accessor below rather
than a module-scope import, because `main` imports this module to mount the
router and a top-level import back would be a cycle. Those helpers move out
of `main` as later steps of the split land.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api.deps import (
    get_current_user,
    get_redis,
    get_session_dep,
    get_settings_dep,
)
from vts.api.schemas import (
    TaskOut,
    UploadConfigOut,
    UploadInitOut,
    UploadInitRequest,
    UploadOffsetOut,
)
from vts.core.config import Settings
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.media import probe_media
from vts.services.storage import task_dir
from vts.services.task_progress import summary_progress_for_task
from vts.services.upload_order import resolve_order
from vts.services.upload_session import UploadSession, part_name
from vts.services.upload_set import UploadSetError, classify_suffixes, verify_probes

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])


def _main():
    """Late-bound access to helpers still living in `vts.api.main`.

    `main` imports this module to mount the router, so importing it back at
    module scope would be a cycle. These are pure helpers with no request
    state; they move here (or to a shared module) as later steps of the split
    land — see docs/plans/main-py-split.md.
    """
    from vts.api import main

    return main


@router.get("/api/uploads/config", response_model=UploadConfigOut)
async def uploads_config(settings: Settings = Depends(get_settings_dep)) -> UploadConfigOut:
    return UploadConfigOut(
        chunked_threshold_bytes=settings.upload_chunked_threshold_bytes,
        chunk_bytes=settings.upload_chunk_bytes,
        max_upload_bytes=settings.max_upload_bytes,
    )


@router.post("/api/uploads/init", response_model=UploadInitOut)
async def uploads_init(
    payload: UploadInitRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    settings: Settings = Depends(get_settings_dep),
) -> UploadInitOut:
    if payload.files:
        if len(payload.files) > settings.upload_max_files:
            raise HTTPException(
                status_code=422,
                detail=f"A set may contain at most {settings.upload_max_files} files",
            )
        if any(f.total_size <= 0 for f in payload.files):
            raise HTTPException(status_code=422, detail="total_size must be positive")
        combined = sum(f.total_size for f in payload.files)
        if combined > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Set exceeds maximum upload size")
        try:
            kind = classify_suffixes([f.filename for f in payload.files])
        except UploadSetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        normalized_prompts = _main()._normalize_prompts_json(payload.prompts)
        if normalized_prompts and not payload.transcript:
            raise HTTPException(status_code=422, detail="prompts require transcript")
        if payload.diarize and not payload.transcript:
            raise HTTPException(status_code=422, detail="diarize requires transcript")
        normalized_delivery = await _validated_delivery(
            session, user, _main()._normalize_delivery_json(payload.delivery)
        )

        upload_id = uuid.uuid4()
        options = {
            "language": payload.language or None,
            "audio_only": False,
            "transcript": payload.transcript,
            "diarize": payload.diarize,
            "prompts": normalized_prompts,
            "delivery": normalized_delivery,
        }
        # Order is resolved at finalize, once creation_time can be probed
        # from the actual bytes. Index here is just selection order.
        spec_files = [
            {
                "filename": f.filename,
                "suffix": Path(f.filename).suffix.lower(),
                "total_size": f.total_size,
                "last_modified": f.last_modified,
            }
            for f in payload.files
        ]
        UploadSession.init_multi(
            settings.artifacts_root, user.username,
            user_id=user.id, upload_id=upload_id, files=spec_files, kind=kind,
            options=options, display_name=_main().normalize_display_name(payload.display_name),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        return UploadInitOut(
            upload_id=str(upload_id),
            chunk_size=settings.upload_chunk_bytes,
            files=[{"index": i, "filename": f["filename"]} for i, f in enumerate(spec_files)],
        )

    suffix = Path(payload.filename).suffix.lower()
    if suffix not in _main()._ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix or '(none)'}")
    if payload.total_size <= 0:
        raise HTTPException(status_code=422, detail="total_size must be positive")
    if payload.total_size > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size")
    normalized_prompts = _main()._normalize_prompts_json(payload.prompts)
    if normalized_prompts and not payload.transcript:
        raise HTTPException(status_code=422, detail="prompts require transcript")
    if payload.diarize and not payload.transcript:
        raise HTTPException(status_code=422, detail="diarize requires transcript")
    normalized_delivery = await _validated_delivery(
        session, user, _main()._normalize_delivery_json(payload.delivery)
    )
    upload_id = uuid.uuid4()
    options = {
        "language": payload.language or None,
        # Always a file:// task: audio_only is a yt-dlp download hint and the
        # download never runs, so the flag is meaningless. Don't take the
        # client's word for it — a stray true would only mislead the UI.
        "audio_only": False,
        "transcript": payload.transcript,
        # Explicit even when false — see /api/tasks/upload for why a missing
        # key is not the same as false here (vts-552).
        "diarize": payload.diarize,
        "prompts": normalized_prompts,
        "delivery": normalized_delivery,
    }
    UploadSession.init(
        settings.artifacts_root, user.username,
        user_id=user.id, upload_id=upload_id, suffix=suffix,
        total_size=payload.total_size, options=options,
        display_name=_main().normalize_display_name(payload.display_name),
        filename=payload.filename,
        created_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    return UploadInitOut(upload_id=str(upload_id), chunk_size=settings.upload_chunk_bytes)


async def _validated_delivery(session, user, entries: list[dict]) -> list[dict]:
    """Validate delivery refs for an upload, as the URL path does.

    Checked at INIT rather than at finalize: the user is standing in front
    of the form now, whereas a finalize failure would arrive after the
    whole file had been uploaded.
    """
    from vts.services.delivery_submit import (
        DeliveryValidationError,
        validate_delivery_refs,
    )

    if not entries:
        return []
    try:
        return await validate_delivery_refs(
            Repo(session), uuid.UUID(user.id), entries
        )
    except DeliveryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _load_owned_session(settings, user, upload_id_str: str):
    try:
        upload_id = uuid.UUID(upload_id_str)
    except ValueError:
        raise HTTPException(status_code=404, detail="Upload not found")
    meta = UploadSession.load(settings.artifacts_root, user.username, upload_id)
    if meta is None or meta.get("user_id") != user.id:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload_id, meta


def _entry_for_index(meta: dict, index: int) -> dict:
    for entry in meta.get("files", []):
        if entry.get("index") == index:
            return entry
    raise HTTPException(status_code=404, detail=f"No file at index {index}")


async def _lock_upload(session: AsyncSession, upload_id: uuid.UUID) -> None:
    """Serialize work for one upload_id across concurrent requests and
    workers (vts-hh1).

    Two simultaneous finalize calls on the same session used to run the
    whole probe/rename sequence in parallel: both passed the completeness
    check, then both ran `_rename_to_concat_order`, whose per-file
    check-then-act (`if exists(): rename()`) is not safe to interleave —
    the loser hits FileNotFoundError renaming a file the winner already
    moved, or worse lands one part's bytes under another part's name.

    `pg_advisory_xact_lock` rather than a Redis lock with a TTL: the
    critical section runs ffprobe over every part of the set and has no
    bounded duration, so any TTL short enough to self-heal after a crash
    is also short enough to expire mid-finalize and re-open the very race
    it was meant to close. A transaction-scoped advisory lock has no TTL
    to tune — Postgres drops it when the transaction ends, including when
    the connection dies, so a crashed worker cannot wedge the upload.

    The lock is released by the caller's commit/rollback, i.e. exactly at
    the end of the request. Each request gets its own session (and so its
    own connection), so this genuinely serializes them.

    The 128-bit UUID is folded into the signed 64-bit key the advisory
    lock API takes. A collision only means two unrelated uploads briefly
    serialize against each other, which is harmless.
    """
    key = int.from_bytes(upload_id.bytes[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


@router.get("/api/uploads/{upload_id}/offset", response_model=UploadOffsetOut)
async def uploads_offset(
    upload_id: str,
    index: int = 0,
    user: AuthenticatedUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> UploadOffsetOut:
    uid, meta = _load_owned_session(settings, user, upload_id)
    if meta.get("files"):
        entry = _entry_for_index(meta, index)
        part = UploadSession.part_path_for(
            settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
        )
        return UploadOffsetOut(
            received=UploadSession.received_bytes(part), total_size=entry["total_size"]
        )
    part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
    return UploadOffsetOut(received=UploadSession.received_bytes(part), total_size=meta["total_size"])


@router.patch("/api/uploads/{upload_id}")
async def uploads_patch(
    upload_id: str,
    request: Request,
    offset: int,
    index: int = 0,
    user: AuthenticatedUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    uid, meta = _load_owned_session(settings, user, upload_id)
    if meta.get("files"):
        entry = _entry_for_index(meta, index)
        part = UploadSession.part_path_for(
            settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
        )
        declared = entry["total_size"]
    else:
        part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
        declared = meta["total_size"]

    current = UploadSession.received_bytes(part)
    if offset != current:
        raise HTTPException(status_code=409, detail=f"Offset mismatch; expected {current}")
    data = await request.body()
    if current + len(data) > declared:
        raise HTTPException(status_code=413, detail="Chunk exceeds declared total_size")
    meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
    if meta.get("files"):
        new_size = await asyncio.to_thread(
            UploadSession.append_chunk_at, part, meta_path, data, index
        )
    else:
        new_size = await asyncio.to_thread(
            UploadSession.append_chunk, part, meta_path, data, declared
        )
    return JSONResponse({"received": new_size})


@router.post("/api/uploads/{upload_id}/finalize", response_model=TaskOut)
async def uploads_finalize(
    upload_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> TaskOut:
    # Parse the id WITHOUT requiring the sidecar to still be on disk:
    # a successful finalize unlinks it, so _load_owned_session would 404
    # a legitimate retry before the already-finalized check below could
    # answer it. Ownership is still enforced — get_task_for_user filters
    # by user_id, and _load_owned_session runs (and 404s) as before for
    # anything that is not an already-finalized upload of this user's.
    try:
        uid = uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Upload not found")

    # Serialize the whole finalize sequence per upload_id (vts-hh1).
    # Taken BEFORE the sidecar is read, so the loser of a race reads it
    # only after the winner has committed and unlinked it, and therefore
    # observes the finished state rather than a stale snapshot taken
    # before the winner started.
    await _lock_upload(session, uid)

    # An already-finalized upload is not an error: the loser of a
    # concurrent pair (or a client retry after a dropped response) should
    # get the task that exists, not a primary-key crash or a 404 from the
    # sidecar the winner has already unlinked. Checked under the lock, so
    # "does a task exist" cannot change while we look.
    existing_repo = Repo(session)
    existing = await existing_repo.get_task_for_user(uuid.UUID(user.id), uid)
    if existing is not None:
        # No set_committed_value(..., "steps", []) here, unlike the
        # freshly-created path below: get_task_for_user eagerly loads the
        # real steps, and a retry may well arrive after the pipeline has
        # started producing them.
        queue_positions = await _main()._get_cached_queue_positions(
            redis, existing_repo, settings.redis_prefix
        )
        lane_positions = await _main()._get_lane_positions(redis, settings.redis_prefix)
        asr_progress = await existing_repo.get_asr_progress_for_tasks([existing.id])
        summary_progress = {existing.id: summary_progress_for_task(existing)}
        return _main().serialize_task(
            existing, queue_positions, asr_progress, summary_progress, lane_positions
        )

    uid, meta = _load_owned_session(settings, user, upload_id)

    if meta.get("files"):
        media_dir = Path(settings.artifacts_root) / _main()._user_hash_dir(user.username) / str(uid) / "media"

        # Recover any `ordered.NNN.*` leftovers from an interrupted
        # concat-order reorder (vts-vm0 blocker 2, "second, dirtier
        # window"): a crash between the two rename passes below leaves
        # files under a name nothing else recognises. `ordered.NNN.*`
        # already encodes the target concat-order position in its own
        # name — finishing pass 2 for it needs no re-probing and no
        # re-resolution of order, so this is safe to do unconditionally,
        # before the completeness check, on every finalize call (a no-op
        # when there is nothing to recover). This can never collide with
        # entries not yet reordered: `ordered.*` is a distinct name
        # prefix from `audio.original.*`, and each `position` is unique
        # across the whole set.
        def _finish_interrupted_reorder() -> None:
            for stray in sorted(media_dir.glob("ordered.*")):
                target = media_dir / part_name(
                    int(stray.stem.split(".")[1]), stray.suffix
                )
                stray.rename(target)

        if media_dir.exists():
            await asyncio.to_thread(_finish_interrupted_reorder)

        # Whether a PREVIOUS (crashed) attempt already got far enough to
        # persist the concat-order decision (see below). Used to skip
        # completeness/finalize_multi/probing work that already
        # succeeded once and can no longer be safely redone by name —
        # once the reorder has run, files may no longer sit at the
        # selection-index name those steps look for (vts-vm0 blocker 2).
        already_resolved = meta.get("resolved_order") is not None

        # Every part must be complete before anything else is worth
        # doing. A part already renamed to its final (selection-index)
        # name by a previous, crashed finalize call also satisfies this
        # — otherwise every retry past that rename would 409 forever
        # even though the bytes are safely on disk. If resolved_order is
        # already persisted, completeness was already proven at that
        # point (probing every final succeeded and verify_probes
        # passed), so this check is skipped entirely rather than
        # wrongly reporting "incomplete" for an upload that is actually
        # fully finalized and just hasn't reached the Task-row commit
        # yet.
        if not already_resolved:
            for entry in meta["files"]:
                part = UploadSession.part_path_for(
                    settings.artifacts_root, user.username, uid, entry["index"], entry["suffix"]
                )
                if UploadSession.received_bytes(part) == entry["total_size"]:
                    continue
                final = media_dir / part_name(entry["index"], entry["suffix"])
                if final.exists() and final.stat().st_size == entry["total_size"]:
                    continue
                raise HTTPException(
                    status_code=409, detail=f"Upload incomplete: {entry['filename']}"
                )

        # remove_meta=False: the sidecar is what makes this session
        # findable/retryable and what keeps find_abandoned_sessions()
        # from treating the directory as GC-eligible. Probing, set
        # validation and the concat-order rename are all still ahead and
        # can fail (or the process can die) before a Task row exists, so
        # the sidecar stays until the row is actually committed below.
        #
        # Once resolved_order is persisted, finalize_multi has already
        # done its job for good on a previous attempt — every `.part`
        # it could ever rename is gone, and by this point the reorder
        # rename may have moved finals off their selection-index name
        # entirely, which is the only name finalize_multi knows how to
        # look for. Calling it again here would raise "missing staging
        # file" for a set that is not missing anything.
        if not already_resolved:
            try:
                finals = await asyncio.to_thread(
                    UploadSession.finalize_multi,
                    settings.artifacts_root, user.username, uid, meta,
                    remove_meta=False,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        else:
            finals = []

        # The order decision (vts-vm0 blocker 2): resolve_order's result
        # depends on probing files that the reorder rename below is about
        # to move (and that a PREVIOUS, crashed finalize call may already
        # have moved). Re-probing and re-resolving on every retry would
        # mean re-identifying which on-disk file is which entry once the
        # reorder has run — the selection-index name the probe loop
        # above matched against is gone by then. So the decision is made
        # exactly ONCE and persisted into the upload.json sidecar right
        # after it is computed, before any reorder rename touches disk. A
        # retry that finds a persisted decision reuses it verbatim
        # instead of re-deriving it, which is what makes the reorder
        # rename below safe to redo/resume no matter where a previous
        # attempt crashed inside it.
        resolved = meta.get("resolved_order")
        if not already_resolved:
            try:
                probes = await asyncio.to_thread(
                    lambda: [(e["filename"], probe_media(p)) for e, p in zip(meta["files"], finals)]
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            try:
                verify_probes(meta["kind"], probes)
            except UploadSetError as exc:
                # Log the rejection with the parameters that clashed, so we
                # can answer "how often do real users hit this?" before
                # deciding whether re-encoding (vts-3ow) is worth building.
                # Without this the only signal is a user complaint.
                logging.getLogger(__name__).warning(
                    "upload set rejected as incompatible: kind=%s files=%d reason=%s parts=%s",
                    meta["kind"],
                    len(probes),
                    exc,
                    [
                        {
                            "name": name,
                            "video": probe.video_signature() if probe.has_video else None,
                            "audio": probe.audio_signature(),
                        }
                        for name, probe in probes
                    ],
                )
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            entries = [
                {
                    "filename": entry["filename"],
                    "creation_time": probe.creation_time,
                    "last_modified": entry.get("last_modified"),
                    "index": entry["index"],
                    "duration_sec": probe.duration_sec,
                }
                for entry, (_, probe) in zip(meta["files"], probes)
            ]
            ordered, order_source = resolve_order(entries)
            resolved = {"ordered": ordered, "order_source": order_source}

            def _persist_resolved_order() -> None:
                meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
                on_disk = json.loads(meta_path.read_text(encoding="utf-8"))
                on_disk["resolved_order"] = resolved
                meta_path.write_text(
                    json.dumps(on_disk, ensure_ascii=True, indent=2), encoding="utf-8"
                )

            await asyncio.to_thread(_persist_resolved_order)

        ordered = resolved["ordered"]
        order_source = resolved["order_source"]

        # Rename to concat order: a later task globs the finals in name
        # order, so the index in the name IS the order (vts-vm0). Staging
        # names are in SELECTION order, and resolve_order's result may be a
        # permutation of that — a naive one-pass rename can collide (renaming
        # A onto a name B still occupies destroys B), so go through
        # temporary `ordered.NNN.*` names first.
        #
        # Resumable (vts-vm0 blocker 2): a previous crashed attempt may
        # have already moved some or all entries into `ordered.*` (pass
        # 1) or all the way to their final concat-order name (pass 2, or
        # the `ordered.*` recovery pass above). Pass 1 only fires for an
        # entry whose selection-index file still exists — note this is
        # NOT the same test as "does the concat-order target already
        # exist", because a sibling entry's selection-index file can
        # legitimately still be sitting at that same name (e.g. b at
        # selection index 0 occupies audio.original.000.* while a, at
        # position 0, has not been moved there yet) — checking the
        # wrong thing would wrongly skip a's move and leave b's bytes
        # under a's name. Pass 2 is naturally idempotent: its only
        # precondition is "source is at ordered.*", a namespace nothing
        # else ever writes to, so re-running it is always safe.
        def _rename_to_concat_order() -> None:
            pending: list[tuple[int, Path]] = []
            for position, item in enumerate(ordered):
                suffix = Path(item["filename"]).suffix.lower()
                current = media_dir / part_name(item["index"], suffix)
                ordered_name = media_dir / f"ordered.{position:03d}{suffix}"
                if current.exists():
                    current.rename(ordered_name)
                pending.append((position, ordered_name))
            for position, ordered_name in pending:
                if ordered_name.exists():
                    ordered_name.rename(media_dir / part_name(position, ordered_name.suffix))

        await asyncio.to_thread(_rename_to_concat_order)

        options = dict(meta["options"])
        options["source_files"] = [
            {"name": item["filename"], "offset_sec": 0.0, "duration_sec": item["duration_sec"]}
            for item in ordered
        ]
        options["source_files_order"] = order_source
        options["source_files_kind"] = meta["kind"]

        repo = Repo(session)
        artifact = task_dir(settings.artifacts_root, user.username, uid)
        task = await repo.create_task(
            user_id=uuid.UUID(user.id),
            source_url=f"file://{ordered[0]['filename']}",
            options=options,
            artifact_dir=str(artifact),
            task_id=uid,
            source_title=meta.get("display_name"),
        )
        await session.commit()
        # Only now is the Task row real: safe to remove the sidecar that
        # made this session retryable up to this point (vts-vm0).
        meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
        try:
            await asyncio.to_thread(meta_path.unlink)
        except OSError:
            pass
        return await _main()._enqueue_uploaded_task(task, repo, redis, settings)

    part = UploadSession.part_path(settings.artifacts_root, user.username, uid, meta["suffix"])
    if UploadSession.received_bytes(part) != meta["total_size"]:
        raise HTTPException(status_code=409, detail="Upload incomplete")
    meta_path = UploadSession.meta_path(settings.artifacts_root, user.username, uid)
    await asyncio.to_thread(UploadSession.finalize, part, meta["suffix"], meta_path)
    repo = Repo(session)
    artifact = task_dir(settings.artifacts_root, user.username, uid)
    source_url = f"file://{Path(meta['filename']).name}"
    task = await repo.create_task(
        user_id=uuid.UUID(user.id),
        source_url=source_url,
        options=meta["options"],
        artifact_dir=str(artifact),
        task_id=uid,
        source_title=meta.get("display_name"),
    )
    await session.commit()
    return await _main()._enqueue_uploaded_task(task, repo, redis, settings)

"""Chunked-upload session storage (vts-b8j).

State lives entirely on local disk under artifacts_root:
  <task_dir>/upload.json                    -- session metadata sidecar
  <task_dir>/media/audio.original<suffix>.part  -- staging file (chunks appended)

No DB row exists until finalize. Pure path/file logic — no FastAPI/DB imports —
so it unit-tests on a tmp dir. task_dir layout matches vts.services.storage.task_dir.
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from vts.services.storage import task_dir


def _media_name(suffix: str) -> str:
    # suffix includes the leading dot; result e.g. audio.original.mp4
    return f"audio.original{suffix}"


def part_name(index: int, suffix: str) -> str:
    """Indexed staging name, e.g. audio.original.000.mp4.

    The single-file session used a fixed `audio.original<suffix>`, so N files in
    one session would overwrite each other. The index IS the concat order, so
    globbing the finals in name order gives the right sequence (vts-vm0).

    Note: zero-padding assumes file count << 1000 (1000+ files would sort
    incorrectly: 1000.mp4 < 099.mp4). The API layer enforces upload_max_files.
    """
    return f"audio.original.{index:03d}{suffix}"


class UploadSession:
    @staticmethod
    def _dir(artifacts_root: Path, username: str, upload_id: uuid.UUID) -> Path:
        return task_dir(artifacts_root, username, upload_id)

    @classmethod
    def part_path(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID, suffix: str) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "media" / f"{_media_name(suffix)}.part"

    @classmethod
    def meta_path(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "upload.json"

    @classmethod
    def init(
        cls,
        artifacts_root: Path,
        username: str,
        *,
        user_id: str,
        upload_id: uuid.UUID,
        suffix: str,
        total_size: int,
        options: dict,
        display_name: str | None,
        filename: str,
        created_at: str,
    ) -> Path:
        d = cls._dir(artifacts_root, username, upload_id)
        media = d / "media"
        media.mkdir(parents=True, exist_ok=True)
        part = media / f"{_media_name(suffix)}.part"
        part.touch(exist_ok=True)
        meta = {
            "upload_id": str(upload_id),
            "user_id": user_id,
            "username": username,
            "suffix": suffix,
            "total_size": total_size,
            "received": 0,
            "options": options,
            "display_name": display_name,
            "filename": filename,
            "created_at": created_at,
        }
        cls.meta_path(artifacts_root, username, upload_id).write_text(
            json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8"
        )
        return d

    @classmethod
    def load(cls, artifacts_root: Path, username: str, upload_id: uuid.UUID) -> dict | None:
        p = cls.meta_path(artifacts_root, username, upload_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    @staticmethod
    def received_bytes(part_path: Path) -> int:
        return part_path.stat().st_size if part_path.exists() else 0

    @staticmethod
    def append_chunk(part_path: Path, meta_path: Path, data: bytes, total_size: int) -> int:
        with open(part_path, "ab") as f:
            f.write(data)
        new_size = part_path.stat().st_size
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["received"] = new_size
            meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass
        return new_size

    @staticmethod
    def finalize(part_path: Path, suffix: str, meta_path: Path) -> Path:
        final = part_path.with_name(_media_name(suffix))
        part_path.rename(final)  # same dir/volume -> atomic
        try:
            meta_path.unlink()
        except OSError:
            pass
        return final

    @classmethod
    def part_path_for(
        cls, artifacts_root: Path, username: str, upload_id: uuid.UUID,
        index: int, suffix: str,
    ) -> Path:
        return cls._dir(artifacts_root, username, upload_id) / "media" / f"{part_name(index, suffix)}.part"

    @classmethod
    def init_multi(
        cls,
        artifacts_root: Path,
        username: str,
        *,
        user_id: str,
        upload_id: uuid.UUID,
        files: list[dict],
        kind: str,
        options: dict,
        display_name: str | None,
        created_at: str,
    ) -> Path:
        """Stage N parts under one session. `files` is already in concat order."""
        d = cls._dir(artifacts_root, username, upload_id)
        media = d / "media"
        media.mkdir(parents=True, exist_ok=True)

        entries = []
        for index, item in enumerate(files):
            (media / f"{part_name(index, item['suffix'])}.part").touch(exist_ok=True)
            entries.append({
                "index": index,
                "filename": item["filename"],
                "suffix": item["suffix"],
                "total_size": item["total_size"],
                "last_modified": item.get("last_modified"),
                "received": 0,
            })

        meta = {
            "upload_id": str(upload_id),
            "user_id": user_id,
            "username": username,
            "kind": kind,
            "files": entries,
            "total_size": sum(e["total_size"] for e in entries),
            "options": options,
            "display_name": display_name,
            "created_at": created_at,
        }
        cls.meta_path(artifacts_root, username, upload_id).write_text(
            json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8"
        )
        return d

    @staticmethod
    def append_chunk_at(part_path: Path, meta_path: Path, data: bytes, index: int) -> int:
        with open(part_path, "ab") as f:
            f.write(data)
        new_size = part_path.stat().st_size
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for entry in meta.get("files", []):
                if entry.get("index") == index:
                    entry["received"] = new_size
                    break
            meta_path.write_text(json.dumps(meta, ensure_ascii=True, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            pass
        return new_size

    @classmethod
    def finalize_multi(
        cls, artifacts_root: Path, username: str, upload_id: uuid.UUID, meta: dict,
        *, remove_meta: bool = True,
    ) -> list[Path]:
        """Rename every staging part to its final name, in index order.

        Pre-checks that all expected .part files exist before renaming any of them,
        so the session is left intact and retryable on any error.

        `remove_meta=False` keeps the `upload.json` sidecar alive after the
        rename. The caller (uploads_finalize) still has probing, set
        validation and a concat-order rename ahead of it before a Task row
        exists — all of which can fail or the process can die mid-way. The
        sidecar is what makes a session findable/retryable and what makes
        find_abandoned_sessions() treat the directory as live rather than
        eligible for GC (vts-vm0); unlinking it here, before that work is
        done, would strand the upload with no way back if anything after
        this call fails. The default stays True so Task 5's tests and any
        other caller that finalizes in one uninterruptible step keep their
        existing behaviour.
        """
        media = cls._dir(artifacts_root, username, upload_id) / "media"
        entries = sorted(meta.get("files", []), key=lambda e: e["index"])

        # Verify all parts exist before renaming any (atomic-ish: either all or none)
        missing = []
        for entry in entries:
            final = media / part_name(entry["index"], entry["suffix"])
            part = media / f"{final.name}.part"
            if not part.exists():
                missing.append(str(part))
        if missing:
            raise RuntimeError(f"Cannot finalize: missing staging file(s): {', '.join(missing)}")

        # All parts exist; now rename them
        finals: list[Path] = []
        for entry in entries:
            final = media / part_name(entry["index"], entry["suffix"])
            part = media / f"{final.name}.part"
            part.rename(final)  # same dir/volume -> atomic
            finals.append(final)
        if remove_meta:
            try:
                cls.meta_path(artifacts_root, username, upload_id).unlink()
            except OSError:
                pass
        return finals


def find_abandoned_sessions(artifacts_root: Path, *, ttl_seconds: int) -> dict[uuid.UUID, Path]:
    """Task dirs holding an `upload.json` older than `ttl_seconds`.

    The sidecar is the whole safety argument: `finalize()` unlinks it *before*
    the Task row is created, so its presence means no task ever came of this
    upload. A directory without one is live data and is never a candidate.

    Every ambiguity fails closed — an unreadable sidecar, an unparseable
    timestamp or a non-UUID directory name all mean "not a candidate". The
    caller must still confirm no Task row exists before deleting.
    """
    log = logging.getLogger("vts.upload_gc")
    if not artifacts_root.is_dir():
        return {}

    now = datetime.now(tz=timezone.utc)
    found: dict[uuid.UUID, Path] = {}

    for user_dir in artifacts_root.iterdir():
        if not user_dir.is_dir():
            continue
        for candidate in user_dir.iterdir():
            if not candidate.is_dir():
                continue
            try:
                upload_id = uuid.UUID(candidate.name)
            except ValueError:
                continue  # not a task dir — never ours to delete
            meta_path = candidate / "upload.json"
            if not meta_path.is_file():
                continue  # finalized (or never a session): live data

            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(meta["created_at"])
            except (ValueError, OSError, KeyError, TypeError):
                log.warning("upload-gc: unreadable session metadata, skipping %s", candidate)
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() < ttl_seconds:
                continue  # still within the TTL: an upload may be in progress

            found[upload_id] = candidate

    return found


def delete_abandoned_sessions(
    candidates: dict[uuid.UUID, Path],
    *,
    has_task: Callable[[uuid.UUID], bool],
) -> list[uuid.UUID]:
    """Delete the given abandoned sessions. Returns the ids actually removed.

    Takes the candidates rather than re-scanning: the caller checks the Task
    rows for exactly this set, and a second scan could turn up a directory that
    was never checked against the DB — which would be deleted unverified.

    `has_task` is the last gate: a directory whose id owns a Task row is real
    artifacts, never a leftover. That cannot happen by construction (see
    find_abandoned_sessions), but deletion is irreversible so it is checked.
    """
    log = logging.getLogger("vts.upload_gc")
    removed: list[uuid.UUID] = []

    for upload_id, path in candidates.items():
        if has_task(upload_id):
            log.warning("upload-gc: %s has a task row despite upload.json, skipping", upload_id)
            continue
        # Re-check the sidecar: a finalize may have landed since the scan, in
        # which case this is now a real task's media, not a leftover.
        if not (path / "upload.json").is_file():
            log.info("upload-gc: %s was finalized since the scan, skipping", upload_id)
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            log.warning("upload-gc: failed to remove %s", path, exc_info=True)
            continue
        removed.append(upload_id)
        log.info("upload-gc: removed abandoned session %s", upload_id)

    return removed


def purge_abandoned_sessions(
    artifacts_root: Path,
    *,
    ttl_seconds: int,
    has_task: Callable[[uuid.UUID], bool],
) -> list[uuid.UUID]:
    """Scan and delete in one call. Convenience wrapper for tests/scripts; the
    worker uses find_ + delete_ so the DB check covers exactly what it deletes."""
    return delete_abandoned_sessions(
        find_abandoned_sessions(artifacts_root, ttl_seconds=ttl_seconds),
        has_task=has_task,
    )

"""Chunked-upload session storage (vts-b8j).

State lives entirely on local disk under artifacts_root:
  <task_dir>/upload.json                    -- session metadata sidecar
  <task_dir>/media/audio.original<suffix>.part  -- staging file (chunks appended)

No DB row exists until finalize. Pure path/file logic — no FastAPI/DB imports —
so it unit-tests on a tmp dir. task_dir layout matches vts.services.storage.task_dir.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from vts.services.storage import task_dir


def _media_name(suffix: str) -> str:
    # suffix includes the leading dot; result e.g. audio.original.mp4
    return f"audio.original{suffix}"


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

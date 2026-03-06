from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any


def user_hash(username: str) -> str:
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    return digest[:24]


def task_dir(root: Path, username: str, task_id: uuid.UUID) -> Path:
    return root / user_hash(username) / str(task_id)


def ensure_task_dirs(base: Path) -> dict[str, Path]:
    paths = {
        "root": base,
        "media": base / "media",
        "segments": base / "segments",
        "outputs": base / "outputs",
        "logs": base / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def cow_copy_dir(src: Path, dst: Path) -> None:
    """Copy src directory to dst using CoW (reflink) when supported, falling back to regular copy.

    dst must already exist as an empty directory.
    """
    import subprocess

    result = subprocess.run(
        ["cp", "-a", "--reflink=auto", f"{src}/.", str(dst)],
        capture_output=True,
    )
    if result.returncode != 0:
        # Fallback: pure-Python copy (no reflink)
        import shutil

        shutil.copytree(str(src), str(dst), dirs_exist_ok=True)


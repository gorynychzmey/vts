"""Leaf helpers with no dependencies of their own.

Kept separate so the other helper modules can layer on top without importing
each other: `SUMMARY_STEP_NAMES` is needed by both the serialization and the
artifact-filesystem helpers, and `_find_media_file` by serialization, artifacts
and the routers.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUMMARY_STEP_NAMES = frozenset(
    {
        "prepare_llama_model",
        "prepare_summary_chunks",
        "summarize_windows",
        "pack_window_notes",
        "summarize_final",
    }
)

def _is_path_within(root: Path, path: Path) -> bool:
    try:
        root_resolved = root.resolve()
        path_resolved = path.resolve()
    except OSError:
        return False
    return path_resolved == root_resolved or root_resolved in path_resolved.parents

def _user_hash_dir(username: str) -> str:
    from vts.services.storage import user_hash
    return user_hash(username)

def _find_media_file(artifact_dir: str | None) -> Path | None:
    if not artifact_dir:
        return None
    media_dir = Path(artifact_dir) / "media"
    # audio.combined.* rather than a fixed .wav: a stream-copied set keeps the
    # uploaded encoding, so the extension varies (vts-08q). It must still come
    # after video.mkv and before the raw parts.
    for pattern in ("video.mkv", "audio.combined.*", "audio.original.*"):
        matches = sorted(
            p for p in (media_dir.glob(pattern) if media_dir.exists() else [])
            # Skip our own probe sidecar (audio.original.*.probe.json), which
            # the wildcard would otherwise pick up as the "media" file.
            if not p.name.endswith(".probe.json")
        )
        if matches:
            return matches[-1]
    return None

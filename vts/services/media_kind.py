from __future__ import annotations

from pathlib import Path
from typing import Literal

MediaKind = Literal["video", "audio"]

_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v",
})
_AUDIO_SUFFIXES = frozenset({
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma",
})

_MIME_BY_SUFFIX: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".mkv":  "video/x-matroska",
    ".webm": "video/webm",
    ".avi":  "video/x-msvideo",
    ".mov":  "video/quicktime",
    ".wmv":  "video/x-ms-wmv",
    ".flv":  "video/x-flv",
    ".ts":   "video/mp2t",
    ".m4v":  "video/x-m4v",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
    ".ogg":  "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav":  "audio/wav",
    ".wma":  "audio/x-ms-wma",
}


def media_kind(path: Path) -> MediaKind:
    suffix = path.suffix.lower()
    if suffix in _AUDIO_SUFFIXES:
        return "audio"
    return "video"


def media_content_type(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL


ProgressCallback = Callable[[str, dict[str, Any]], None]


class _YdlLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def debug(self, msg: str) -> None:
        self.logger.info("yt-dlp %s", msg)

    def info(self, msg: str) -> None:
        self.logger.info("yt-dlp %s", msg)

    def warning(self, msg: str) -> None:
        self.logger.warning("yt-dlp %s", msg)

    def error(self, msg: str) -> None:
        self.logger.error("yt-dlp %s", msg)


def _run_download(
    *,
    url: str,
    outtmpl: str,
    ydl_opts: dict[str, Any],
    phase: str,
    progress_cb: ProgressCallback,
    logger: logging.Logger,
) -> None:
    def hook(data: dict[str, Any]) -> None:
        status = data.get("status", "")
        if status != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        progress = float(downloaded) / float(total) if total else 0.0
        progress_cb(
            phase,
            {
                "phase": phase,
                "progress": progress,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
            },
        )

    options = dict(ydl_opts)
    options["outtmpl"] = outtmpl
    options["progress_hooks"] = [hook]
    options["logger"] = _YdlLogger(logger)
    with YoutubeDL(options) as ydl:
        ydl.download([url])


def download_video_and_audio(
    *,
    source_url: str,
    media_dir: Path,
    progress_cb: ProgressCallback,
    logger: logging.Logger,
) -> tuple[Path, Path]:
    media_dir.mkdir(parents=True, exist_ok=True)
    video_out = media_dir / "video.%(ext)s"
    audio_out = media_dir / "audio.%(ext)s"

    logger.info("downloading video stream")
    _run_download(
        url=source_url,
        outtmpl=str(video_out),
        ydl_opts={
            "format": "bv*+ba/best",
            "noplaylist": True,
            "quiet": True,
            "merge_output_format": "mp4",
        },
        phase="video",
        progress_cb=progress_cb,
        logger=logger,
    )

    logger.info("downloading audio stream")
    _run_download(
        url=source_url,
        outtmpl=str(audio_out),
        ydl_opts={
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
        },
        phase="audio",
        progress_cb=progress_cb,
        logger=logger,
    )

    video_file = next(media_dir.glob("video.*"), None)
    audio_file = next(media_dir.glob("audio.*"), None)
    if not video_file or not audio_file:
        raise RuntimeError("yt-dlp did not produce expected video/audio files")
    return video_file, audio_file

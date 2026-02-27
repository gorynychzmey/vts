from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_dlp import YoutubeDL


ProgressCallback = Callable[[str, dict[str, Any]], None]
PhaseCallback = Callable[[str, str], None]
YOUTUBE_CLIENT_FALLBACK_ORDER = ("android_vr", "android", "ios", "mweb", "web_safari", "web")


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


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "youtu.be":
        return True
    return (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


def _build_youtube_client_candidates(
    *,
    preferred_client: str | None,
    configured_client: str | None,
) -> list[str]:
    if configured_client and configured_client.strip():
        return [configured_client.strip()]
    candidates: list[str] = []
    if preferred_client and preferred_client.strip():
        candidates.append(preferred_client.strip())
    for candidate in YOUTUBE_CLIENT_FALLBACK_ORDER:
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _with_youtube_player_client(ydl_opts: dict[str, Any], player_client: str | None) -> dict[str, Any]:
    if not player_client:
        return dict(ydl_opts)
    options = dict(ydl_opts)
    extractor_args = dict(options.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = [player_client]
    extractor_args["youtube"] = youtube_args
    options["extractor_args"] = extractor_args
    return options


def _run_download_with_client_resolution(
    *,
    url: str,
    outtmpl: str,
    ydl_opts: dict[str, Any],
    phase: str,
    progress_cb: ProgressCallback,
    logger: logging.Logger,
    preferred_youtube_client: str | None,
    configured_youtube_client: str | None,
) -> str | None:
    if not _is_youtube_url(url):
        _run_download(
            url=url,
            outtmpl=outtmpl,
            ydl_opts=ydl_opts,
            phase=phase,
            progress_cb=progress_cb,
            logger=logger,
        )
        return None

    candidates = _build_youtube_client_candidates(
        preferred_client=preferred_youtube_client,
        configured_client=configured_youtube_client,
    )
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates, start=1):
        options = _with_youtube_player_client(ydl_opts, candidate)
        logger.info("yt-dlp youtube player client attempt %s/%s: %s", index, len(candidates), candidate)
        try:
            _run_download(
                url=url,
                outtmpl=outtmpl,
                ydl_opts=options,
                phase=phase,
                progress_cb=progress_cb,
                logger=logger,
            )
            return candidate
        except Exception as exc:
            last_error = exc
            if index >= len(candidates):
                raise
            logger.warning("yt-dlp youtube player client %s failed: %s", candidate, exc)
            logger.info("yt-dlp retrying with next youtube player client")

    if last_error:
        raise last_error
    return None


def _build_ytdlp_base_opts(
    *,
    ytdlp_cookies_file: Path | None,
    ytdlp_cookies_from_browser: list[str],
    ytdlp_youtube_po_token: str | None,
    ytdlp_verbose: bool,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": not ytdlp_verbose,
        "verbose": ytdlp_verbose,
    }
    if ytdlp_cookies_file:
        opts["cookiefile"] = str(ytdlp_cookies_file)
    browser_spec = tuple(item.strip() for item in ytdlp_cookies_from_browser if item.strip())
    if browser_spec:
        opts["cookiesfrombrowser"] = browser_spec
    youtube_args: dict[str, list[str]] = {}
    if ytdlp_youtube_po_token and ytdlp_youtube_po_token.strip():
        youtube_args["po_token"] = [ytdlp_youtube_po_token.strip()]
    if youtube_args:
        opts["extractor_args"] = {"youtube": youtube_args}
    return opts


def _run_process(command: list[str], logger: logging.Logger) -> None:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    if proc.stdout:
        for line in proc.stdout.splitlines():
            if line.strip():
                logger.info("ffmpeg %s", line)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            if line.strip():
                logger.info("ffmpeg %s", line)
    if proc.returncode != 0:
        raise RuntimeError(f"Process failed ({proc.returncode}): {' '.join(command)}")


def _find_single(media_dir: Path, pattern: str) -> Path:
    matches = sorted(media_dir.glob(pattern))
    if not matches:
        raise RuntimeError(f"Expected file matching {pattern}")
    return matches[-1]


def _probe_audio_codec(path: Path) -> str:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}")
    codec = proc.stdout.strip().splitlines()
    if not codec:
        raise RuntimeError(f"Unable to detect audio codec for {path}")
    return codec[0].strip().lower()


def _codec_extension(codec: str) -> str:
    mapping = {
        "aac": "m4a",
        "opus": "opus",
        "vorbis": "ogg",
        "flac": "flac",
        "alac": "m4a",
        # Keep "no mp3 artifact" contract; use a generic container for mp3 streams.
        "mp3": "mka",
    }
    return mapping.get(codec, "mka")


def download_video_and_audio(
    *,
    source_url: str,
    media_dir: Path,
    progress_cb: ProgressCallback,
    phase_cb: PhaseCallback,
    logger: logging.Logger,
    audio_only: bool = False,
    preferred_youtube_client: str | None = None,
    ytdlp_cookies_file: Path | None = None,
    ytdlp_cookies_from_browser: list[str] | None = None,
    ytdlp_youtube_player_client: str | None = None,
    ytdlp_youtube_po_token: str | None = None,
    ytdlp_verbose: bool = False,
) -> tuple[Path | None, Path, str | None]:
    media_dir.mkdir(parents=True, exist_ok=True)
    video_source_out = media_dir / "video.source.%(ext)s"
    audio_source_out = media_dir / "audio.source.%(ext)s"
    video_merged = media_dir / "video.mkv"
    common_ydl_opts = _build_ytdlp_base_opts(
        ytdlp_cookies_file=ytdlp_cookies_file,
        ytdlp_cookies_from_browser=ytdlp_cookies_from_browser or [],
        ytdlp_youtube_po_token=ytdlp_youtube_po_token,
        ytdlp_verbose=ytdlp_verbose,
    )

    if audio_only:
        phase_cb("audio", "running")
        logger.info("downloading audio stream")
        selected_client = _run_download_with_client_resolution(
            url=source_url,
            outtmpl=str(audio_source_out),
            ydl_opts={
                **common_ydl_opts,
                "format": "bestaudio/best",
            },
            phase="audio",
            progress_cb=progress_cb,
            logger=logger,
            preferred_youtube_client=preferred_youtube_client,
            configured_youtube_client=ytdlp_youtube_player_client,
        )
        phase_cb("audio", "done")
        audio_source = _find_single(media_dir, "audio.source.*")
        phase_cb("postprocess", "running")
        codec = _probe_audio_codec(audio_source)
        audio_original = media_dir / f"audio.original.{_codec_extension(codec)}"
        _run_process(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(audio_source),
                "-vn",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                str(audio_original),
            ],
            logger,
        )
        phase_cb("postprocess", "done")
        audio_source.unlink(missing_ok=True)
        return None, audio_original, selected_client

    phase_cb("video", "running")
    logger.info("downloading video stream")
    selected_video_client = _run_download_with_client_resolution(
        url=source_url,
        outtmpl=str(video_source_out),
        ydl_opts={
            **common_ydl_opts,
            "format": "bestvideo/best",
        },
        phase="video",
        progress_cb=progress_cb,
        logger=logger,
        preferred_youtube_client=preferred_youtube_client,
        configured_youtube_client=ytdlp_youtube_player_client,
    )
    phase_cb("video", "done")

    phase_cb("audio", "running")
    logger.info("downloading audio stream")
    selected_audio_client = _run_download_with_client_resolution(
        url=source_url,
        outtmpl=str(audio_source_out),
        ydl_opts={
            **common_ydl_opts,
            "format": "bestaudio/best",
        },
        phase="audio",
        progress_cb=progress_cb,
        logger=logger,
        preferred_youtube_client=selected_video_client or preferred_youtube_client,
        configured_youtube_client=ytdlp_youtube_player_client,
    )
    phase_cb("audio", "done")

    video_source = _find_single(media_dir, "video.source.*")
    audio_source = _find_single(media_dir, "audio.source.*")

    phase_cb("merge", "running")
    logger.info("merging downloaded streams into %s", video_merged.name)
    _run_process(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_source),
            "-i",
            str(audio_source),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            str(video_merged),
        ],
        logger,
    )
    phase_cb("merge", "done")

    phase_cb("postprocess", "running")
    codec = _probe_audio_codec(video_merged)
    audio_original = media_dir / f"audio.original.{_codec_extension(codec)}"
    logger.info("extracting original audio stream with copy codec -> %s", audio_original.name)
    _run_process(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_merged),
            "-vn",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            str(audio_original),
        ],
        logger,
    )
    phase_cb("postprocess", "done")

    video_source.unlink(missing_ok=True)
    audio_source.unlink(missing_ok=True)
    selected_client = selected_audio_client or selected_video_client
    return video_merged, audio_original, selected_client

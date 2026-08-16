from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vts.core.failures import classify_failure_code


ProgressCallback = Callable[[str, dict[str, Any]], None]
PhaseCallback = Callable[[str, str], None]
# The dict yt-dlp hands its progress hooks; now rebuilt from the child process.
ProgressHook = Callable[[dict[str, Any]], None]
# Grace period for the download child to exit before escalating SIGTERM->SIGKILL.
_CHILD_EXIT_TIMEOUT = 10.0
# How many trailing stderr lines to keep for the failure message. Bounded
# because ffmpeg can write megabytes there on a long HLS download (vts-u9ap),
# and only the tail ever reaches a human.
_STDERR_TAIL_LINES = 200
# Live download children, so a cancelled task can actually stop the download.
_CHILDREN: set[subprocess.Popen[str]] = set()
_CHILDREN_LOCK = threading.Lock()
YOUTUBE_CLIENT_FALLBACK_ORDER = ("android_vr", "android", "ios", "mweb", "web_safari", "web")
_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")


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


def _extract_download_progress(data: dict[str, Any]) -> tuple[float, int | float, int | float]:
    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
    downloaded = data.get("downloaded_bytes") or 0
    if total:
        progress = float(downloaded) / float(total)
        return max(0.0, min(1.0, progress)), downloaded, total

    percent_raw = data.get("_percent_str")
    if isinstance(percent_raw, str):
        match = _PERCENT_RE.search(percent_raw)
        if match:
            try:
                progress = float(match.group(1)) / 100.0
                return max(0.0, min(1.0, progress)), downloaded, total
            except ValueError:
                pass
    return 0.0, downloaded, total


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
        info_dict = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}
        media_title = str(info_dict.get("title", "")).strip() if info_dict else ""
        raw_filename = data.get("filename") or info_dict.get("_filename")
        media_filename = Path(raw_filename).name if isinstance(raw_filename, str) and raw_filename.strip() else ""
        meta = {}
        if media_title:
            meta["media_title"] = media_title
        if media_filename:
            meta["media_filename"] = media_filename
        if status == "finished":
            progress_cb(phase, {"phase": phase, "progress": 1.0, **meta})
            return
        if status != "downloading":
            return
        progress, downloaded, total = _extract_download_progress(data)
        progress_cb(
            phase,
            {
                "phase": phase,
                "progress": progress,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                **meta,
            },
        )

    _run_download_subprocess(
        url=url,
        outtmpl=outtmpl,
        ydl_opts=ydl_opts,
        phase=phase,
        hook=hook,
        logger=logger,
    )


def _run_download_subprocess(
    *,
    url: str,
    outtmpl: str,
    ydl_opts: dict[str, Any],
    phase: str,
    hook: ProgressHook,
    logger: logging.Logger,
) -> None:
    """Run yt-dlp in a child process, replaying its progress and logs here.

    Isolating the download (vts-xkx4) needs an egress rule, and the kernel can
    only name a process — not the asyncio.to_thread thread this used to be.
    The child is otherwise ordinary: same options, same progress hook, same
    exception text, so _run_download_with_client_resolution above keeps
    classifying failures exactly as before.
    """
    request = json.dumps(
        {
            "url": url,
            "outtmpl": outtmpl,
            # cookiesfrombrowser arrives as a tuple; JSON makes it a list, and
            # yt-dlp accepts either.
            "ydl_opts": ydl_opts,
            "phase": phase,
        }
    )
    command = [sys.executable, "-m", "vts.services.ytdlp_runner"]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    _register_child(proc)

    # Drain stderr on its own thread rather than after the stdout loop (vts-u9ap).
    # stderr is a ~64KB pipe. yt-dlp's own logger is captured into the stdout
    # protocol, so the normal case is quiet — but on HLS/DASH/live sources
    # yt-dlp shells out to ffmpeg, which inherits fd 2 and reports progress
    # there, outside that logger. Once the pipe fills the child blocks in
    # write(), so it never closes stdout and the read loop below waits forever.
    # Only the tail is kept: it exists for the error message, and an ffmpeg-
    # chatty download would otherwise buffer megabytes to no purpose.
    stderr_tail_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    stderr_reader: threading.Thread | None = None
    if proc.stderr is not None:
        stderr_stream = proc.stderr

        def _drain_stderr() -> None:
            try:
                for err_line in stderr_stream:
                    stderr_tail_lines.append(err_line)
            except (ValueError, OSError):
                # Closed from under us while the child was being killed.
                pass

        stderr_reader = threading.Thread(
            target=_drain_stderr, name="ytdlp-stderr", daemon=True
        )
        stderr_reader.start()

    failure: str | None = None
    try:
        proc.stdin.write(request)
        proc.stdin.close()
        # Read as it arrives rather than communicate(): progress must reach SSE
        # while the download runs, not after it ends.
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.warning("yt-dlp runner emitted non-JSON line: %s", line[:200])
                continue
            kind = message.get("t")
            if kind == "progress":
                hook(_rehydrate_progress(message.get("payload") or {}))
            elif kind == "log":
                _log_from_child(logger, message)
            elif kind == "error":
                failure = str(message.get("message") or "yt-dlp failed")
    except BaseException:
        # Covers the parent dying mid-read (broken pipe, worker shutdown), NOT
        # task cancellation: measured, asyncio.to_thread only abandons the
        # await, so nothing is ever raised in here when a task is cancelled.
        # Stopping a cancelled download is kill_active_downloads()'s job.
        _terminate(proc, logger)
        raise
    finally:
        # Normal path: the child has closed stdout, so this returns promptly.
        # Cancellation path: _terminate already reaped it, and wait() is a no-op.
        try:
            proc.wait(timeout=_CHILD_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp runner did not exit after stdout closed; killing")
            _terminate(proc, logger)
        # The child has exited by now, so its end of the pipe is closed and the
        # reader is at EOF; the timeout is only a backstop so a wedged reader
        # cannot pin the worker thread.
        if stderr_reader is not None:
            stderr_reader.join(timeout=_CHILD_EXIT_TIMEOUT)
        if proc.stderr:
            try:
                proc.stderr.close()
            except OSError:
                pass
        stderr_tail = "".join(stderr_tail_lines).strip()
        _unregister_child(proc)

    if failure is not None:
        # Re-raised as a plain RuntimeError carrying the child's message: the
        # caller only ever reads str(exc), via classify_failure_code().
        raise RuntimeError(failure)
    if proc.returncode != 0:
        detail = stderr_tail[-500:] if stderr_tail else f"exit code {proc.returncode}"
        raise RuntimeError(f"yt-dlp runner failed: {detail}")


def _register_child(proc: subprocess.Popen[str]) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)


def _unregister_child(proc: subprocess.Popen[str]) -> None:
    with _CHILDREN_LOCK:
        _CHILDREN.discard(proc)


def kill_active_downloads(logger: logging.Logger | None = None) -> int:
    """Kill any yt-dlp child still running, returning how many were stopped.

    Cancelling the asyncio task is not enough on its own: asyncio.to_thread
    only abandons the *await*, and the worker thread runs to completion (this
    was equally true when yt-dlp ran inline as a thread — a cancelled download
    kept downloading, just invisibly). Now that it is a process, cancellation
    finally has something it can act on, but somebody has to pull the trigger.

    Called from the cancel path in the worker, which owns the decision; this
    module only knows how to stop what it started.
    """
    log = logger or logging.getLogger(__name__)
    with _CHILDREN_LOCK:
        victims = list(_CHILDREN)
    stopped = 0
    for proc in victims:
        if proc.poll() is None:
            _terminate(proc, log)
            stopped += 1
    return stopped


def _terminate(proc: subprocess.Popen[str], logger: logging.Logger) -> None:
    """Stop the child, escalating to SIGKILL if it ignores SIGTERM."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_CHILD_EXIT_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp runner ignored SIGTERM; sending SIGKILL")
    proc.kill()
    try:
        proc.wait(timeout=_CHILD_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Unkillable means stuck in uninterruptible I/O; nothing more to do
        # here, and blocking the worker thread forever would be worse.
        logger.error("yt-dlp runner survived SIGKILL")


def _rehydrate_progress(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the shape yt-dlp's own progress hook would have passed.

    The child cannot ship info_dict wholesale (it holds arbitrary objects), so
    it sends the handful of fields the hook actually reads and they are put
    back into place here — keeping _extract_download_progress and the title
    capture working on the structure they already expect.
    """
    info_dict = {}
    if payload.get("info_title"):
        info_dict["title"] = payload["info_title"]
    if payload.get("info_filename"):
        info_dict["_filename"] = payload["info_filename"]
    return {
        "status": payload.get("status", ""),
        "downloaded_bytes": payload.get("downloaded_bytes"),
        "total_bytes": payload.get("total_bytes"),
        "total_bytes_estimate": payload.get("total_bytes_estimate"),
        "_percent_str": payload.get("_percent_str"),
        "filename": payload.get("filename"),
        "info_dict": info_dict,
    }


def _log_from_child(logger: logging.Logger, message: dict[str, Any]) -> None:
    text = str(message.get("msg") or "")
    level = message.get("level")
    if level == "error":
        logger.error("yt-dlp %s", text)
    elif level == "warning":
        logger.warning("yt-dlp %s", text)
    else:
        logger.info("yt-dlp %s", text)


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
            failure_code = classify_failure_code(str(exc))
            if failure_code == "download_live_not_started":
                logger.warning("yt-dlp non-retriable youtube error (%s): %s", failure_code, exc)
                raise
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

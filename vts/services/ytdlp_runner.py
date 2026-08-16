"""Child process that runs yt-dlp and reports back over stdout as JSON lines.

Split out of the worker for vts-xkx4: yt-dlp used to run via asyncio.to_thread,
so it shared the worker's PID, UID and network namespace — the same namespace
that legitimately reaches Postgres, Redis, whisper and litellm. Nothing the
kernel can filter on distinguishes a thread, so the download had to become a
process before any egress rule could name it.

This module is that process. It is deliberately thin: it downloads, and it
prints. Every decision that used to live around the download — which YouTube
player client to try next, whether an error is retriable — stays in the parent
(downloader.py), because those depend on classify_failure_code() reading the
real error text rather than whatever lands on stderr.

Protocol (one JSON object per line on stdout):
    {"t": "progress", "phase": ..., "payload": {...}}   download progress
    {"t": "log", "level": "info"|"warning"|"error", "msg": ...}
    {"t": "ok"}                                         download finished
    {"t": "error", "message": ..., "cls": ...}          download raised

stdout carries only the protocol; anything yt-dlp writes directly goes to
stderr so a stray print cannot corrupt the stream. The parent reads the
"error" line rather than the exit code, because it needs the message text.

Input arrives as a single JSON object on stdin — not argv — so the PO-token
and cookie paths never show up in the process table.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _emit(obj: dict[str, Any]) -> None:
    """Write one protocol line. Flushed immediately: the parent streams these
    into SSE, so buffering them would stall the progress bar until the end."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _ProtocolLogger:
    """Stands in for the parent's _YdlLogger, forwarding over the pipe."""

    def debug(self, msg: str) -> None:
        _emit({"t": "log", "level": "info", "msg": str(msg)})

    def info(self, msg: str) -> None:
        _emit({"t": "log", "level": "info", "msg": str(msg)})

    def warning(self, msg: str) -> None:
        _emit({"t": "log", "level": "warning", "msg": str(msg)})

    def error(self, msg: str) -> None:
        _emit({"t": "log", "level": "error", "msg": str(msg)})


def _jsonable(value: Any) -> Any:
    """yt-dlp's info_dict holds arbitrary objects; keep only what survives JSON."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def run(request: dict[str, Any]) -> int:
    # Imported here, not at module scope: the parent imports nothing from this
    # module, and keeping yt-dlp out of the import path until it is actually
    # needed keeps the failure mode obvious if the sandbox lacks it.
    from yt_dlp import YoutubeDL

    url = request["url"]
    phase = request["phase"]
    options = dict(request["ydl_opts"])
    options["outtmpl"] = request["outtmpl"]

    def hook(data: dict[str, Any]) -> None:
        # Mirrors the parent's hook, but ships the raw fields instead of the
        # computed progress: _extract_download_progress stays in the parent so
        # its behaviour (and its tests) are unaffected by the process split.
        info_dict = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}
        payload = {
            "status": data.get("status", ""),
            "downloaded_bytes": _jsonable(data.get("downloaded_bytes")),
            "total_bytes": _jsonable(data.get("total_bytes")),
            "total_bytes_estimate": _jsonable(data.get("total_bytes_estimate")),
            "_percent_str": _jsonable(data.get("_percent_str")),
            "filename": _jsonable(data.get("filename")),
            "info_title": _jsonable((info_dict or {}).get("title")),
            "info_filename": _jsonable((info_dict or {}).get("_filename")),
        }
        _emit({"t": "progress", "phase": phase, "payload": payload})

    options["progress_hooks"] = [hook]
    options["logger"] = _ProtocolLogger()

    try:
        with YoutubeDL(options) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 — the parent classifies, we only report
        # The message matters more than the exit code: the parent feeds it to
        # classify_failure_code() to decide whether to try the next player
        # client or give up (download_live_not_started).
        _emit({"t": "error", "message": str(exc), "cls": type(exc).__name__})
        return 1
    _emit({"t": "ok"})
    return 0


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
    except Exception as exc:  # noqa: BLE001
        _emit({"t": "error", "message": f"bad request: {exc}", "cls": "ProtocolError"})
        return 2
    return run(request)


if __name__ == "__main__":
    raise SystemExit(main())

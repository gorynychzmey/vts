"""OBS Studio script: upload the last recording to VTS when recording stops.

Drop this file into OBS Studio via Tools → Scripts → +.

Configuration is read once from environment variables when OBS loads the
script. Restart OBS (or re-load the script in the Tools → Scripts dialog)
after changing env vars.

Env vars
--------
VTS_BASE_URL     — required, e.g. "https://vts.vostrikov.dev" (no trailing slash)
VTS_API_TOKEN    — required, the "vts_…" personal API token
VTS_TRANSCRIPT   — "true"/"false", default "true"
VTS_SUMMARY      — "true"/"false", default "true"  (requires transcript=true)
VTS_LANGUAGE     — "" / "ru" / "en" / "de" / "fr" / …  (empty = auto-detect)
VTS_AUDIO_ONLY   — "true"/"false", default "false"

Why env vars and not OBS script properties: simpler to share one config
file (e.g. ~/.config/obs-studio/vts.env sourced before launching OBS)
across machines than to copy script settings via OBS UI.

Limitations
-----------
- Uploads in a background thread, but the whole file is loaded into RAM
  to build the multipart body. For typical OBS recordings (15-60 min,
  720p MP4 → 200-1500 MB) this is fine on modern machines. For larger
  files a streamed http.client.HTTPSConnection rewrite would be needed.
- Only handles RECORDING_STOPPED. The Replay Buffer's "Saved" event is
  a separate hook (OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED) — easy to
  add when needed.
"""

from __future__ import annotations

import os
import ssl
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import obspython as obs  # type: ignore[import-not-found]  # provided by OBS at runtime


# ---------------------------------------------------------------- config

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_config() -> dict[str, object]:
    base = os.environ.get("VTS_BASE_URL", "").rstrip("/")
    token = os.environ.get("VTS_API_TOKEN", "").strip()
    return {
        "base_url": base,
        "token": token,
        "transcript": _env_bool("VTS_TRANSCRIPT", True),
        "summary": _env_bool("VTS_SUMMARY", True),
        "language": os.environ.get("VTS_LANGUAGE", "").strip(),
        "audio_only": _env_bool("VTS_AUDIO_ONLY", False),
    }


_config: dict[str, object] = {}


# ---------------------------------------------------------------- multipart

def _build_multipart_body(
    file_path: Path,
    form_fields: dict[str, str],
) -> tuple[bytes, str]:
    """Build a multipart/form-data body. Returns (body_bytes, content_type)."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []

    for key, value in form_fields.items():
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"'.encode())
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    parts.append(f"--{boundary}".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream")
    parts.append(b"")
    parts.append(file_path.read_bytes())
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")

    body = crlf.join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


# ---------------------------------------------------------------- upload

def _upload_blocking(file_path: Path, cfg: dict[str, object]) -> None:
    """Runs on a background thread — must not touch OBS APIs."""
    base_url = str(cfg["base_url"])
    token = str(cfg["token"])

    form: dict[str, str] = {
        "transcript": "true" if cfg["transcript"] else "false",
        "summary": "true" if cfg["summary"] else "false",
        "audio_only": "true" if cfg["audio_only"] else "false",
    }
    language = str(cfg["language"])
    if language:
        form["language"] = language

    body, content_type = _build_multipart_body(file_path, form)
    req = urllib.request.Request(
        f"{base_url}/api/tasks/upload",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
            print(f"[obs_to_vts] upload OK: HTTP {resp.status} for {file_path.name}")
    except urllib.error.HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print(f"[obs_to_vts] upload FAILED: HTTP {e.code}: {body_preview}")
    except (urllib.error.URLError, OSError) as e:
        print(f"[obs_to_vts] upload FAILED: network error: {e}")


def _kick_off_upload(file_path: Path) -> None:
    cfg = dict(_config)
    if not cfg.get("base_url") or not cfg.get("token"):
        print("[obs_to_vts] skipping upload: VTS_BASE_URL or VTS_API_TOKEN not set")
        return
    if not file_path.exists():
        print(f"[obs_to_vts] skipping upload: file not found: {file_path}")
        return
    threading.Thread(
        target=_upload_blocking,
        args=(file_path, cfg),
        daemon=True,
        name="obs-to-vts-upload",
    ).start()
    print(f"[obs_to_vts] uploading {file_path.name} → {cfg['base_url']}")


# ---------------------------------------------------------------- OBS hooks

def _on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        path_str = obs.obs_frontend_get_last_recording()
        if not path_str:
            print("[obs_to_vts] recording stopped but no last recording path")
            return
        _kick_off_upload(Path(path_str))


def script_description():
    return (
        "Upload finished OBS recordings to VTS via the /api/tasks/upload "
        "endpoint. Configure via env vars VTS_BASE_URL, VTS_API_TOKEN, "
        "VTS_TRANSCRIPT, VTS_SUMMARY, VTS_LANGUAGE, VTS_AUDIO_ONLY. "
        "See scripts/obs/README.md for details."
    )


def script_load(_settings):  # pyright: ignore[reportUnusedParameter]
    global _config
    _config = _read_config()
    if not _config["base_url"] or not _config["token"]:
        print(
            "[obs_to_vts] WARNING: VTS_BASE_URL or VTS_API_TOKEN not set in the "
            "environment OBS was launched from. Uploads will be skipped."
        )
    else:
        print(
            f"[obs_to_vts] loaded; target={_config['base_url']}, "
            f"transcript={_config['transcript']}, summary={_config['summary']}, "
            f"audio_only={_config['audio_only']}, "
            f"language={_config['language'] or 'auto'}"
        )
    obs.obs_frontend_add_event_callback(_on_frontend_event)


def script_unload():
    # OBS removes our callback on unload automatically; nothing to do.
    pass

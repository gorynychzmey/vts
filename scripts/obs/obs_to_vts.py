"""OBS Studio script: upload the last recording to VTS when recording stops.

Drop this file into OBS Studio via Tools → Scripts → +.

Configuration is read from two sources, in this order of precedence:

1. **OBS script properties** (Tools → Scripts → select this script).
   Fields shown in the OBS UI; persisted in OBS' own JSON config. Best
   for normal interactive use — no terminal, no env vars, no reboot.
2. **Environment variables** (fallback). Used when the corresponding UI
   field is empty / unset. Useful for scripted / headless / CI setups
   where you launch OBS from a wrapper script.

A non-empty UI value always wins over the env var with the same name.

Env vars (also the names of the UI fields):
  VTS_BASE_URL     — e.g. "https://vts.vostrikov.dev" (no trailing slash)
  VTS_API_TOKEN    — the "vts_…" personal API token
  VTS_TRANSCRIPT   — bool, default true
  VTS_SUMMARY      — bool, default true  (requires transcript=true)
  VTS_LANGUAGE     — "" / "ru" / "en" / "de" / "fr" / …  ("" = auto-detect)
  VTS_AUDIO_ONLY   — bool, default false
  VTS_NOTIFY       — bool, default true  (show desktop notification on
                     upload result; uses notify-send/osascript/PowerShell
                     toast depending on platform; no-op if unavailable)

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
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import obspython as obs  # type: ignore[import-not-found]  # provided by OBS at runtime


# ---------------------------------------------------------------- config

_PROP_BASE_URL = "vts_base_url"
_PROP_API_TOKEN = "vts_api_token"
_PROP_TRANSCRIPT = "vts_transcript"
_PROP_SUMMARY = "vts_summary"
_PROP_LANGUAGE = "vts_language"
_PROP_AUDIO_ONLY = "vts_audio_only"
_PROP_NOTIFY = "vts_notify"
_PROP_DISPLAY_NAME = "vts_display_name_template"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _pick_str(ui_value: str, env_name: str) -> str:
    """UI value wins if non-empty; otherwise fall back to the env var."""
    ui = (ui_value or "").strip()
    if ui:
        return ui
    return os.environ.get(env_name, "").strip()


def _render_display_name(template: str, file_path: Path) -> str:
    """Render the task title the user wants shown in VTS.

    The template may contain the placeholder ``{filename}`` (the recording's
    name without extension). An empty template yields an empty string, which
    the caller treats as "send no display_name" so VTS falls back to the
    file:// source label. Unknown placeholders are left intact rather than
    raising, so a typo never breaks an upload.
    """
    template = (template or "").strip()
    if not template:
        return ""
    stem = file_path.stem
    try:
        return template.format(filename=stem).strip()
    except (KeyError, IndexError, ValueError):
        return template.strip()


def _read_config(settings) -> dict[str, object]:
    """Merge OBS UI properties with env-var fallbacks.

    `settings` is the obs_data_t passed by script_update/script_load.
    For booleans, OBS' obs_data_get_bool returns False on a missing
    key, so we explicitly check obs_data_has_user_value to distinguish
    "user set false" from "never touched" — only in the latter case do
    we fall back to the env var.
    """
    base = _pick_str(obs.obs_data_get_string(settings, _PROP_BASE_URL), "VTS_BASE_URL").rstrip("/")
    token = _pick_str(obs.obs_data_get_string(settings, _PROP_API_TOKEN), "VTS_API_TOKEN")
    language = _pick_str(obs.obs_data_get_string(settings, _PROP_LANGUAGE), "VTS_LANGUAGE")
    display_name_template = _pick_str(
        obs.obs_data_get_string(settings, _PROP_DISPLAY_NAME), "VTS_DISPLAY_NAME_TEMPLATE"
    )

    def _bool(prop: str, env_name: str, default: bool) -> bool:
        if obs.obs_data_has_user_value(settings, prop):
            return bool(obs.obs_data_get_bool(settings, prop))
        return _env_bool(env_name, default)

    return {
        "base_url": base,
        "token": token,
        "transcript": _bool(_PROP_TRANSCRIPT, "VTS_TRANSCRIPT", True),
        "summary": _bool(_PROP_SUMMARY, "VTS_SUMMARY", True),
        "language": language,
        "audio_only": _bool(_PROP_AUDIO_ONLY, "VTS_AUDIO_ONLY", False),
        "notify": _bool(_PROP_NOTIFY, "VTS_NOTIFY", True),
        "display_name_template": display_name_template,
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


# ---------------------------------------------------------------- notifications

def _notify_command(title: str, message: str) -> list[str] | None:
    """Pick a per-platform CLI command for a desktop notification.

    Returns None if no usable notifier is found for this platform —
    callers fall back to log-only.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        if shutil.which("notify-send"):
            return ["notify-send", "--app-name=OBS → VTS", title, message]
        return None
    if platform == "darwin":
        # AppleScript is always present on macOS.
        script = (
            f'display notification {message!r} '
            f'with title {title!r}'
        )
        return ["osascript", "-e", script]
    if platform.startswith("win"):
        if not shutil.which("powershell.exe"):
            return None
        # Native Windows 10/11 toast via WinRT. The single-line PS script
        # uses an empty Toast template, so it doesn't depend on extra
        # modules like BurntToast. Newlines in message must be encoded.
        msg = message.replace('"', '`"').replace("\n", " ")
        hdr = title.replace('"', '`"').replace("\n", " ")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(0);"
            f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('{hdr}'))|Out-Null;"
            f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode('{msg}'))|Out-Null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('OBS → VTS').Show($n)"
        )
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-Command", ps,
        ]
    return None


# subprocess.CREATE_NO_WINDOW is defined only on Windows in the stdlib.
# Use the integer literal so the module still imports on Linux/macOS
# (where the constant is missing). We only pass this flag when running
# on Windows, but referencing it at module scope must not crash.
_CREATE_NO_WINDOW = 0x08000000


def _notify(title: str, message: str, *, enabled: bool) -> None:
    """Best-effort desktop notification. Never raises; silently no-ops on
    platforms without a usable notifier, or when the user disabled them.

    The OBS script log line is always written regardless — this is just
    an additional channel for the user."""
    if not enabled:
        return
    cmd = _notify_command(title, message)
    if cmd is None:
        return
    # On Windows, suppress the console flash from powershell.exe by
    # creating the child process with CREATE_NO_WINDOW. The flag is a
    # no-op (and not accepted) on other platforms.
    extra: dict[str, object] = {}
    if sys.platform.startswith("win"):
        extra["creationflags"] = _CREATE_NO_WINDOW
    try:
        subprocess.run(
            cmd, check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **extra,
        )
    except (OSError, subprocess.SubprocessError):
        # Don't let a notifier hiccup interrupt the upload thread.
        pass


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

    display_name = _render_display_name(str(cfg.get("display_name_template", "")), file_path)
    if display_name:
        form["display_name"] = display_name

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
    notify_enabled = bool(cfg.get("notify", True))
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
            print(f"[obs_to_vts] upload OK: HTTP {resp.status} for {file_path.name}")
            _notify(
                "VTS upload OK",
                f"Uploaded {file_path.name}",
                enabled=notify_enabled,
            )
    except urllib.error.HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print(f"[obs_to_vts] upload FAILED: HTTP {e.code}: {body_preview}")
        _notify(
            "VTS upload failed",
            f"HTTP {e.code} for {file_path.name}",
            enabled=notify_enabled,
        )
    except (urllib.error.URLError, OSError) as e:
        print(f"[obs_to_vts] upload FAILED: network error: {e}")
        _notify(
            "VTS upload failed",
            f"Network error: {e}",
            enabled=notify_enabled,
        )


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
        "Upload finished OBS recordings to VTS via /api/tasks/upload. "
        "Fill in the fields below, or leave them blank to fall back to "
        "the matching VTS_* env vars. See scripts/obs/README.md for details."
    )


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, _PROP_BASE_URL, "")
    obs.obs_data_set_default_string(settings, _PROP_API_TOKEN, "")
    obs.obs_data_set_default_string(settings, _PROP_LANGUAGE, "")
    obs.obs_data_set_default_string(settings, _PROP_DISPLAY_NAME, "")
    obs.obs_data_set_default_bool(settings, _PROP_TRANSCRIPT, True)
    obs.obs_data_set_default_bool(settings, _PROP_SUMMARY, True)
    obs.obs_data_set_default_bool(settings, _PROP_AUDIO_ONLY, False)
    obs.obs_data_set_default_bool(settings, _PROP_NOTIFY, True)


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_text(
        props, _PROP_BASE_URL, "VTS base URL", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_text(
        props, _PROP_API_TOKEN, "API token (vts_…)", obs.OBS_TEXT_PASSWORD
    )
    obs.obs_properties_add_bool(props, _PROP_TRANSCRIPT, "Generate transcript")
    obs.obs_properties_add_bool(props, _PROP_SUMMARY, "Generate summary")
    obs.obs_properties_add_bool(props, _PROP_AUDIO_ONLY, "Audio only")
    obs.obs_properties_add_bool(
        props, _PROP_NOTIFY,
        "Desktop notification on upload result",
    )
    obs.obs_properties_add_text(
        props, _PROP_LANGUAGE,
        'Language ("" = auto, or "ru" / "en" / "de" / …)',
        obs.OBS_TEXT_DEFAULT,
    )
    obs.obs_properties_add_text(
        props, _PROP_DISPLAY_NAME,
        'Task name template ("" = filename; {filename} = recording name)',
        obs.OBS_TEXT_DEFAULT,
    )
    return props


def _log_loaded_summary() -> None:
    if not _config.get("base_url") or not _config.get("token"):
        print(
            "[obs_to_vts] WARNING: base URL or API token not configured "
            "(neither OBS UI fields nor VTS_* env vars). Uploads will be skipped."
        )
        return
    # Never log the token itself.
    print(
        f"[obs_to_vts] config: target={_config['base_url']}, "
        f"transcript={_config['transcript']}, summary={_config['summary']}, "
        f"audio_only={_config['audio_only']}, "
        f"language={_config['language'] or 'auto'}, "
        f"name_template={_config.get('display_name_template') or '(filename)'}"
    )


def script_load(settings):
    global _config
    _config = _read_config(settings)
    _log_loaded_summary()
    obs.obs_frontend_add_event_callback(_on_frontend_event)


def script_update(settings):
    """OBS fires this each time the user edits a property in the UI."""
    global _config
    _config = _read_config(settings)
    _log_loaded_summary()


def script_unload():
    # OBS removes our callback on unload automatically; nothing to do.
    pass

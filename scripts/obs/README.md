# OBS Studio → VTS uploader

`obs_to_vts.py` is an OBS Studio script that uploads each finished
recording to your VTS instance via `/api/tasks/upload`. Auth uses a
personal API token (see [docs/AUTH.md](../../docs/AUTH.md#personal-api-tokens)).

## Prerequisites: Python interpreter

**OBS Studio does not ship with Python.** You must install one yourself
and point OBS at it; otherwise the script silently won't appear in the
scripts list — OBS doesn't show an error message in that state, which is
the usual source of confusion.

Supported versions (per [OBS docs](https://docs.obsproject.com/scripting)):
**Python 3.6 – 3.12**. Python 3.13+ is not yet supported. Architecture
must match OBS — 64-bit OBS needs 64-bit Python.

### Install Python

- **Linux:** usually already installed (`python3 --version` should print
  something between 3.6 and 3.12). If it's 3.13+, install an older
  series via your package manager (e.g. `apt install python3.12` on
  Debian/Ubuntu, then point OBS at it as described below).
- **macOS:** [python.org installer](https://www.python.org/downloads/macos/)
  for Python 3.12; or `brew install python@3.12`.
- **Windows:** [python.org installer](https://www.python.org/downloads/windows/)
  for **64-bit** Python 3.12. During install, leave the default
  install location and untick "Add to PATH" if you only need it for OBS.

### Tell OBS where Python lives

1. OBS → Tools → Scripts → switch to the **Python Settings** tab
   (it's the second tab in the dialog, easy to miss).
2. Click **Browse** and pick the **folder** containing the Python
   install — not the interpreter executable itself.

   Typical paths:
   - **Linux** (system Python): `/usr` (when `python3` is `/usr/bin/python3`).
     For a non-default version installed alongside the system one,
     find it with `which python3.12 | xargs dirname | xargs dirname`.
   - **macOS** (python.org installer): `/Library/Frameworks/Python.framework/Versions/3.12`.
   - **Windows**: e.g. `C:\Users\<you>\AppData\Local\Programs\Python\Python312`.

3. Switch back to the **Scripts** tab. You should now be able to add
   the script with `+`.

## Setup

1. **Generate an API token in VTS.** Open the VTS UI → key icon in the
   header → "Create token" → give it a name (e.g. `obs-laptop`) → copy
   the `vts_…` value once. The raw value is never shown again.

2. **Install the script.** OBS Studio → Tools → Scripts → `+` →
   pick `scripts/obs/obs_to_vts.py`. (Make sure the Python prerequisites
   above are met first — without them the `+` button might still let you
   pick the file but no configuration UI will appear on the right.)

3. **Configure.** Two paths — pick whichever fits:
   - **Easy path:** fill the fields shown on the **right side of the
     Scripts dialog** (VTS base URL, API token, etc.). Changes take
     effect immediately; no OBS restart needed.
   - **Scripted/headless path:** leave the UI fields empty and supply
     the values via env vars (see table below). OBS reads env vars
     once at script load — restart OBS or reload the script after
     changing them.

   You can also mix: UI for the URL, env var for the token, etc. A
   non-empty UI field always wins over the env var with the same name.

4. Check OBS' Script Log panel (Tools → Scripts → Script Log) after
   stopping a recording. You should see one of:
   - `[obs_to_vts] uploading <file>.mkv → https://...`
   - `[obs_to_vts] upload OK: HTTP 200 …`

   If you don't see any `[obs_to_vts]` lines at all (not even on script
   load), Python is probably not configured — go back to *Prerequisites*
   above.

## Settings (UI field name = env var name)

| OBS UI field / Env var | Required | Default | Notes |
|------------------------|----------|---------|-------|
| `VTS_BASE_URL`    | yes | — | e.g. `https://vts.example.com`, no trailing slash |
| `VTS_API_TOKEN`   | yes | — | The `vts_…` token created in the VTS UI (UI field is password-masked) |
| `VTS_TRANSCRIPT`  | no  | `true` | Run the transcription pipeline |
| `VTS_SUMMARY`     | no  | `true` | Run the LLM summary; requires transcript=true |
| `VTS_LANGUAGE`    | no  | (empty = auto) | Force ASR language: `ru`, `en`, `de`, `fr`, … |
| `VTS_AUDIO_ONLY`  | no  | `false` | Skip the video stream during processing |
| `VTS_NOTIFY`      | no  | `true` | Show a desktop notification on upload result. Uses `notify-send` (Linux), `osascript` (macOS), or PowerShell toast (Windows). Silently no-op if the platform notifier isn't available. |
| `VTS_DISPLAY_NAME_TEMPLATE` | no | (empty = filename) | Task name shown in VTS. Plain text, or use the `{filename}` placeholder (recording name without extension). Empty → VTS falls back to the uploaded file name. Example: `OBS: {filename}` → `OBS: 2026-06-01 22-57-08`. |

Env-var bool values: `true` / `false` / `1` / `0` / `yes` / `no` (case-insensitive).

UI values are persisted in OBS' own config (`~/.config/obs-studio/...`
on Linux; equivalent on macOS/Windows). The token field is stored in
clear in that JSON file — same security as any OBS plugin setting.

## Setting env vars (only if you use the scripted path)

OBS inherits the environment of whatever launched it; the *system* env
isn't enough on most desktops.

### Linux

If you launch OBS from a terminal, just `export VTS_BASE_URL=…` etc.
beforehand. For a permanent setup with the .desktop launcher:

```ini
# ~/.local/share/applications/obs.desktop (copy from /usr/share/applications)
Exec=env VTS_BASE_URL=https://vts.example.com VTS_API_TOKEN=vts_xxxx obs
```

Or wrap OBS in a small launcher script:

```bash
#!/usr/bin/env bash
# ~/bin/obs-with-vts
source ~/.config/obs-studio/vts.env
exec obs "$@"
```

### macOS

Easiest is a launcher script identical to the Linux one and a Dock
shortcut pointing at it. Setting env vars in `~/Library/LaunchAgents/`
also works but is more involved.

### Windows

Set the variables for the user account via *System Properties →
Environment Variables*, then restart OBS. Or launch OBS from a `.bat`
file that does `set VTS_BASE_URL=...` before calling `obs64.exe`.

## What gets uploaded

The script reacts to `OBS_FRONTEND_EVENT_RECORDING_STOPPED` and reads
`obs_frontend_get_last_recording()` for the file path. Whatever format
OBS just wrote (mkv, mp4, mov, …) is uploaded as-is. VTS handles the
container/codec via ffmpeg.

The upload happens on a background thread so OBS' UI stays responsive,
but the entire file body is loaded into RAM to build the multipart
request. Typical OBS recordings (15–60 min, 720p MP4) are 200–1500 MB,
which is fine on modern machines. If you regularly record multi-hour
4K sessions, this script is the wrong tool — consider a desktop daemon
that watches the OBS output folder and streams uploads via `curl`.

The script does **not** delete the local recording after upload. VTS
keeps the original on its end (subject to `media_ttl_hours`, default
72h), so deleting locally is your call.

## Troubleshooting

- **No config fields show up after adding the script, and no
  `[obs_to_vts]` lines in the Script Log.** OBS hasn't loaded the
  script because Python is not configured. See *Prerequisites: Python
  interpreter* above. OBS does **not** show an explicit error for this
  state — the script just silently does nothing.

- **"skipping upload: VTS_BASE_URL or VTS_API_TOKEN not set"** — OBS
  didn't see the env vars. Fill the fields in the OBS Scripts dialog
  instead (easier), or launch OBS from a context where the env vars
  are exported.

- **HTTP 401** — token is wrong, revoked, or the owner was removed
  from the VTS allow-list. Generate a new one in the UI.

- **HTTP 422 "Unsupported file type"** — VTS only accepts a fixed list
  of media extensions (see `_ALLOWED_UPLOAD_SUFFIXES` in
  `vts/api/main.py`). Change the OBS output format to mp4/mkv/mov/etc.

- **Network error / timeout** — large files plus slow upload may
  exceed the 300s urllib timeout. Edit `timeout=300` near the bottom
  of `_upload_blocking` in `obs_to_vts.py`.

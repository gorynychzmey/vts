# vts

Production-ready self-hosted service for video transcription and summarization.

## Documentation map

- Production first-time setup: `docs/INITIAL_DEPLOYMENT.md`
- Spec compliance and key implementation points: `docs/SPEC_COMPLIANCE.md`
- Detailed processing contract audit (download/transcribe/summary): `docs/PROCESSING_CONTRACT.md`
- Workflow and release contract: `PROJECT_RULES.md`
- Runtime example config: `config.yaml`
- systemd runtime env template: `systemd/vts.env.example`

## Stack

- Python 3.14+
- FastAPI (`webapi`) + SSE + minimal SPA
- Async SQLAlchemy + Postgres + Alembic
- Redis queue + pub/sub events (`vts:` prefix)
- Worker pipeline (`yt-dlp`, `ffmpeg`, Whisper API, llama.cpp API)
- Podman containers + systemd units

## Runtime architecture

Containers:

1. `webapi` (`vts.api.main`)
2. `worker` (`vts.worker.main`)
3. Postgres
4. Redis
5. External Whisper ASR webservice
6. External llama.cpp server

Whisper and llama servers are external dependencies and are not implemented in this repository.

## External model services

Install/deploy these separately:

- Whisper ASR webservice:
  - Image: `ghcr.io/ahmetoner/whisper-asr-webservice:latest`
  - Docs: `https://github.com/ahmetoner/whisper-asr-webservice`
- llama.cpp OpenAI-compatible server:
  - Image: `ghcr.io/ggerganov/llama.cpp:server`
  - Docs: `https://github.com/ggerganov/llama.cpp/tree/master/examples/server`

Pipeline includes dedicated `prepare_llama_model` and `prepare_summary_chunks` steps before summary generation. They perform model warm-up and transcript chunk preparation so long tokenization/detokenization is visible as a separate stage.

Summarization uses adaptive token budgeting: token targets for each stage are computed as clamped ratios of the input size rather than fixed paragraph counts. A dedicated `pack_window_notes` step (Stage B) deduplicates and compresses per-window notes before final synthesis when the total exceeds the final-stage context budget. All budget knobs are configurable via `summary_*` settings in `config.yaml`.

## Metrics (JSONL)

Every task run emits structured metrics to a JSONL file (one JSON object per line) and duplicates each event as a single log line.

**Config keys** (all have `VTS_` env prefix):

| Key | Default | Description |
|-----|---------|-------------|
| `metrics_enabled` | `true` | Enable/disable metrics collection |
| `metrics_jsonl_path` | `/opt/vts/logs/metrics.jsonl` | Path to the JSONL output file |
| `metrics_redundancy_shingle_n` | `3` | Word n-gram size for SimHash redundancy |
| `metrics_redundancy_simhash_bits` | `64` | SimHash bit width |
| `metrics_redundancy_max_hamming` | `3` | Max Hamming distance for near-duplicate detection |

**Event stages emitted per task:**

- `download`, `extract_audio`, `trim_initial_silence`, `segment_audio`, `detect_language`, `transcribe_segments`, `merge_transcript`, `prepare_llama_model`, `prepare_summary_chunks`, `summarize_windows`, `pack_window_notes`, `summarize_final` — wall time per pipeline step
- `transcribe.segment` — per-segment ASR: `rtf`, `t_wall_ms`, `t_queue_ms`, `retries`
- `summarize.segment` — per-window LLM: `llm_prompt_tokens`, `llm_completion_tokens`, `llm_tok_per_s`, `compression_ratio`, `redundancy_dup_sentence_ratio`, `number_mismatch_count`, `format`
- `summarize.global` — final summary: same fields as above + `packing_triggered`
- `task.final` — aggregates: `p50/p95` for RTF, tok/s, compression ratio, redundancy; worst-3 by number mismatch and redundancy

**Example JSONL line** (`summarize.segment`):

```json
{"ts":"2026-03-03T12:00:00.000Z","task_id":"abc","run_id":"xyz","stage":"summarize.segment","status":"ok","segment_id":1,"t_wall_ms":9800,"t_queue_ms":120,"llm_prompt_tokens":450,"llm_completion_tokens":180,"llm_total_tokens":630,"llm_tok_per_s":18.37,"llm_ctx_utilization":0.0137,"compression_ratio":0.4,"redundancy_dup_sentence_ratio":0.0,"numbers_in_summary":2,"numbers_in_transcript":3,"number_mismatch_count":0,"dates_in_summary":0,"dates_in_transcript":0,"date_mismatch_count":0,"units_in_summary":1,"units_in_transcript":2,"unit_mismatch_count":0,"format":{"paragraph_count":2,"bullet_ratio":0.0,"heading_count":0,"format_violations":[]},"prompt_version":""}
```

Read the log: `tail -f /opt/vts/logs/metrics.jsonl | python3 -m json.tool`

## yt-dlp YouTube auth and diagnostics

When YouTube returns `HTTP 403`, configure `yt-dlp` runtime options in `config.yaml` (or `VTS_*` overrides):

- `ytdlp_cookies_file` (`VTS_YTDLP_COOKIES_FILE`)
- `ytdlp_cookies_from_browser` (`VTS_YTDLP_COOKIES_FROM_BROWSER`, JSON array in order `[browser, profile, keyring, container]`)
- `ytdlp_youtube_player_client` (`VTS_YTDLP_YOUTUBE_PLAYER_CLIENT`)
- `ytdlp_youtube_po_token` (`VTS_YTDLP_YOUTUBE_PO_TOKEN`)
- `ytdlp_verbose` (`VTS_YTDLP_VERBOSE`)

Worker automatically remembers the last successful YouTube `player_client` per user in DB and reuses it on next tasks.
If saved client fails, worker retries fallback clients and updates stored preference.

## Data model

Tables:

- `users`
- `tasks`
- `steps`
- `asr_segments`
- `asr_words`

Schema is managed by Alembic (`alembic/versions/0001_initial.py`).

## Auth and user context

- API trusts `X-Forwarded-User` only from `trusted_proxy_cidrs`.
- Missing users are auto-created.
- Data is isolated by user id.
- Admin emails are configured by `VTS_ADMIN_EMAILS`.
- Admin can switch context to an existing registered user (`?as_user=<email>`).
- Tasks created while switched are created for the selected user, not the admin.

## PWA: install, share target, push notifications

Installed on Android/desktop (Chromium), the app exposes a [PWA manifest](vts/static/manifest.webmanifest):

- **Share target**: once installed, vts appears in the system share sheet. Sharing a URL (e.g. from YouTube) opens the app with the URL pre-filled in the New Task form. Sharing a video/audio file loads it into the file input. The user still picks options and submits manually.
- **Web Push notifications**: the bell icon in the header asks for notification permission and subscribes the browser. When a task finishes (`completed` or `failed`), the server sends a push. Clicking the notification focuses the app and scrolls to the task.

To enable push, set the VAPID keys on the server:

- `vapid_public_key` (`VTS_VAPID_PUBLIC_KEY`) — base64url-encoded public key, also exposed to the frontend.
- `vapid_private_key` (`VTS_VAPID_PRIVATE_KEY`) — base64url-encoded private key.
- `vapid_subject` (`VTS_VAPID_SUBJECT`) — contact URL for the push service, e.g. `mailto:ops@example.com`.

Generate a keypair once and paste into config:

```
python scripts/generate_vapid_keys.py
```

If VAPID keys are not set, the bell icon stays hidden and push is disabled; the rest of the app works normally.

## Browser cache and auto-update

- `index.html` is served with `Cache-Control: no-store`.
- Frontend assets are versioned (`?v=<server_version>`).
- SPA polls `/api/version` and auto-reloads on version mismatch.

## Production vs local environment files

- Production uses `/opt/vts/config/config.yaml` as the source of truth.
- Production uses `/opt/vts/config/vts.env` mainly for image tag (`VTS_IMAGE`) and optional explicit overrides.
- `.env` / `.env.example` are for local `docker/podman compose` usage and are not required for systemd deployment.

## Build and image publish

`build.sh` builds and pushes one universal image to Docker Hub:

- `docker.io/gorynychzmey/vts:<version>`
- `docker.io/gorynychzmey/vts:latest`

Example:

```bash
docker login
export CONTAINER_ENGINE=docker
export IMAGE_REPO=docker.io/gorynychzmey/vts
export USE_BUILDX=auto
export BUILDX_CACHE_REPO=docker.io/gorynychzmey/vts
export BUILDX_CACHE_MODE=max
export APT_MIRROR=http://deb.debian.org/debian
export APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
./build.sh
```

### Controlled GitHub Actions build

Workflow: `.github/workflows/build-images.yml`

Triggers:

- Manual run: `Actions -> Build Images -> Run workflow`
- Special push tag: `build-*` (for example `build-0.2.1`)
- Team convention: if request says `build` after commit/push, this means pushing `build-*` tag to trigger GitHub Actions build. Local `./build.sh` is run only on explicit request.
- Team convention (strict): `build` after commit/push always means the commit must be accompanied by pushed git tag `build-*`.
- Mandatory rule: before pushing `build-*`, bump version in `vts/__init__.py` and push that commit first.
- Mandatory rule: `build-*` tag version must match current project version.
- Mandatory rule: immediately after pushing `build-*`, start GitHub Actions monitoring in a background subagent.
- Mandatory rule: the subagent must watch the triggered workflow until final status and report the result back into the task.

Tag-trigger example:

```bash
git tag build-0.2.1
git push origin build-0.2.1
```

Notes:

- Build uses existing `build.sh` (including tests inside the built image before push).
- Version source:
  - for tag trigger `build-X.Y.Z`, workflow uses `X.Y.Z` as image version;
  - for manual run you can set input `build_version`;
  - fallback is `vts/__init__.py` version.
- Workflow pushes to both registries:
  - GHCR (primary push from GitHub Actions build)
  - Docker Hub (mirror from GHCR tags)
- Repository targets can be overridden by workflow inputs:
  - `dockerhub_image_repo`
  - `ghcr_image_repo`
- Or by repository variables:
  - `DOCKERHUB_IMAGE_REPO`
  - `GHCR_IMAGE_REPO`
- For Docker Hub pushes set repository secrets:
  - `DOCKERHUB_USERNAME`
  - `DOCKERHUB_TOKEN`
- GHCR push uses built-in `${{ secrets.GITHUB_TOKEN }}`.

### Auto deploy after successful build (optional)

Workflow: `.github/workflows/deploy-after-build.yml`

Trigger:

- Automatically runs after successful `Build Images` workflow.

Required repository secrets:

- `DEPLOY_HOST` (for example `vts.example.com`)
- `DEPLOY_SSH_KEY` (private key for deploy user)
- `DEPLOY_KNOWN_HOSTS` (exact known_hosts line for server key)

Optional repository variables (defaults shown):

- `DEPLOY_USER` (`root`)
- `DEPLOY_PORT` (`22`)
- `DEPLOY_REMOTE_DIR` (`/opt/vts`)
- `DEPLOY_ENV_FILE` (`/opt/vts/config/vts.env`)
- `WEBAPI_SERVICE` (`vts-webapi.service`)
- `WORKER_SERVICE` (`vts-worker.service`)

Prepare `DEPLOY_KNOWN_HOSTS` locally:

```bash
ssh-keyscan -H <your-hostname>
```

## Build performance notes

- Dockerfiles are multi-stage.
- BuildKit caches are used for `apt` and `pip wheel`.
- `build.sh` supports `docker buildx` registry cache (`cache-from` / `cache-to`).
- Image version label is applied at the end of runtime stage, so version bumps do not invalidate heavy `apt`/`pip` layers.
- Runtime images do not include test tooling.
- Local test tooling is in `requirements-dev.txt`.
- On Windows, fastest builds are typically from WSL2 with repository stored in Linux FS (`/home/<user>/...`), not under `C:\...`.

## API summary

- `POST /api/tasks`
- `GET /api/tasks`
- `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/pause`
- `POST /api/tasks/{id}/resume`
- `DELETE /api/tasks/{id}`
- `GET /api/tasks/{id}/transcript`
- `GET /api/tasks/{id}/summary`
- `GET /api/events`
- `GET /api/version`
- `GET /api/me`
- `GET /api/admin/users` (admin only)

## Task options

`POST /api/tasks` supports stage control options:

- `audio_only` (`false` by default): skip video stream download, keep only audio track.
- `transcript` (`true` by default): run transcription pipeline; if `false`, pipeline stops after download.
- `summary` (`true` by default): run summarization pipeline; requires `transcript=true`.
- `language` (`auto`/`ru`/`de`/`en` via UI; free string in API).

Naming note:
- In the detailed contract, `do_transcribe` = `transcript`, `do_summary` = `summary`.
- API accepts both naming styles (`transcript/summary` and `do_transcribe/do_summary`).
- For full behavioral matrix and current gaps vs detailed contract see `docs/PROCESSING_CONTRACT.md`.

## Processing Artifacts

- Download artifacts:
  - `media/video.mkv` (when `audio_only=false`)
  - `media/audio.original.<ext>`
- Segmentation artifacts:
  - `segments/0001.wav`, `segments/0002.wav`, ...
- Transcription artifacts:
  - `asr/segments_raw.json`
  - `outputs/transcript.txt`
- Summary artifacts:
  - `summary/window_01.txt`, `summary/window_02.txt`, ...
  - `summary/windows.json` (per-window notes index)
  - `summary/packed_notes.json` (deduped/packed notes; present when Stage B packing was triggered)
  - `summary/final.md`

## Workflow summary

- Commit flow and semver rules: `PROJECT_RULES.md`
- Deployment flow and server bootstrap: `docs/INITIAL_DEPLOYMENT.md`
- Spec compliance audit and key code entry points: `docs/SPEC_COMPLIANCE.md`
- Detailed processing contract coverage and gap matrix: `docs/PROCESSING_CONTRACT.md`
- Helper scripts:
  - `scripts/bump_version.py`
  - `scripts/prepare_commit.sh` (cleans pytest temp caches, bumps patch, runs tests, stages changes)
  - `build.sh`
  - `deploy.sh`

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

Pipeline includes a dedicated `prepare_llama_model` step before summary generation. It performs model warm-up and emits `llama_model_progress` events; UI shows a spinner while model load/warm-up is in progress.

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

## Browser cache and auto-update

- `index.html` is served with `Cache-Control: no-store`.
- Frontend assets are versioned (`?v=<server_version>`).
- SPA polls `/api/version` and auto-reloads on version mismatch.

## Production vs local environment files

- Production uses `/opt/vts/config/config.yaml` as the source of truth.
- Production uses `/opt/vts/config/vts.env` mainly for image tags (`WEBAPI_IMAGE`, `WORKER_IMAGE`) and optional explicit overrides.
- `.env` / `.env.example` are for local `docker/podman compose` usage and are not required for systemd deployment.

## Build and image publish

`build.sh` builds and pushes both images to Docker Hub:

- `docker.io/gorynychzmey/vts:<version>-webapi`
- `docker.io/gorynychzmey/vts:<version>-worker`
- `docker.io/gorynychzmey/vts:latest-webapi`
- `docker.io/gorynychzmey/vts:latest-worker`

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

Tag-trigger example:

```bash
git tag build-0.2.1
git push origin build-0.2.1
```

Notes:

- Build uses existing `build.sh` (including tests inside `webapi` container before push).
- Workflow pushes to both registries:
  - Docker Hub
  - GHCR
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

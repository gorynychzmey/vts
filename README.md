# vts

Production-ready self-hosted service for video transcription and summarization.

## Documentation map

- Production first-time setup: `docs/INITIAL_DEPLOYMENT.md`
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
export APT_MIRROR=http://ftp.de.debian.org/debian
export APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
./build.sh
```

## Build performance notes

- Dockerfiles are multi-stage.
- BuildKit caches are used for `apt` and `pip wheel`.
- Runtime images do not include test tooling.
- Local test tooling is in `requirements-dev.txt`.

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

## Workflow summary

- Commit flow and semver rules: `PROJECT_RULES.md`
- Deployment flow and server bootstrap: `docs/INITIAL_DEPLOYMENT.md`
- Helper scripts:
  - `scripts/bump_version.py`
  - `scripts/prepare_commit.sh` (cleans pytest temp caches, bumps patch, runs tests, stages changes)
  - `build.sh`
  - `deploy.sh`





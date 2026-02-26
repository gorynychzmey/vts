# vts

Production-ready self-hosted service for video transcription and summarization.

## Stack

- Python 3.14+
- FastAPI (`webapi`) + SSE + minimal SPA
- Async SQLAlchemy + Postgres + Alembic
- Redis queue + pub/sub events (`vts:` prefix)
- Worker pipeline (`yt-dlp`, `ffmpeg`, Whisper API, llama.cpp API)
- Podman containers + systemd units

## Architecture

Containers:

1. `webapi` (`vts.api.main`)
2. `worker` (`vts.worker.main`)
3. Postgres
4. Redis
5. External Whisper ASR webservice
6. External llama.cpp server

Whisper and llama servers are consumed as external endpoints and are not implemented in this repository.

## Data model

Tables:

- `users`
- `tasks`
- `steps`
- `asr_segments`
- `asr_words`

Schema is managed by Alembic (`alembic/versions/0001_initial.py`).

## Authentication model

- API trusts `X-Forwarded-User` only when request source IP is inside `trusted_proxy_cidrs`.
- User record is auto-created on first request.
- All task data is filtered by authenticated user id.
- Admin emails are configured via `VTS_ADMIN_EMAILS`.
- Admin can switch context to another user via `?as_user=<email>` (used by UI admin panel).

## Pipeline DAG

1. `download`
2. `extract_audio`
3. `segment_audio`
4. `transcribe_segments`
5. `merge_transcript`
6. `summarize_windows`
7. `summarize_final`

Each step is restart-safe: persisted in DB, idempotent by output checks, and logs to `logs/task.log`.

## API

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

## Storage layout

- Artifacts: `/srv/vts-data/{user_hash}/{task_id}/`
- Runtime config: `/opt/vts/config.yaml`
- Prompts: `/opt/vts/prompts/segment_prompt.md`, `/opt/vts/prompts/global_prompt.md`

## Admin impersonation

- Set `VTS_ADMIN_EMAILS=["admin1@example.com","admin2@example.com"]`.
- When authenticated user email is in this list, UI shows an Admin Panel.
- Panel allows switching only to users already registered in the system.
- Creating tasks while switched adds tasks to the selected user context, not to admin's own account.
- Current context is always shown as `Working as` in UI header.

## Browser cache and auto-update

- `index.html` is served with `Cache-Control: no-store`.
- Frontend assets are versioned (`/static/app.js?v=<version>`, `/static/styles.css?v=<version>`).
- SPA polls `/api/version`; if server version differs from loaded frontend version, browser auto-reloads to the latest build.
- Hard reload is not required after deployment.

## Quick start

1. Copy env:
   - `cp .env.example .env`
2. Adjust registry/namespace and service URLs.
3. Start stack:
   - `podman compose up -d --build`
4. Apply DB migrations:
   - `alembic upgrade head`
5. Open UI:
   - `http://localhost:8080`

## Versioning policy (strict semver)

Current version lives in `vts/__init__.py`.

Format: `a.b.c`

- Start at `0.0.0`
- Before every commit: bump PATCH `c`
- Before every deployment: bump MINOR `b`, reset PATCH to `0`
- MAJOR `a` reserved for breaking changes

Automation:

- Patch bump: `python scripts/bump_version.py patch`
- Minor bump: `python scripts/bump_version.py minor`
- Version endpoint: `GET /api/version`
- Docker label: `org.opencontainers.image.version`

## Commit workflow

1. `python scripts/bump_version.py patch`
2. `python -m pytest -q`
3. `git add -A`
4. `git commit -m "..."`
5. `git push`

Or use helper:

- `./scripts/prepare_commit.sh`

Task is complete only after tests pass, commit exists, and push is done.

## Deployment workflow (manual)

1. `python scripts/bump_version.py minor`
2. `python -m pytest -q`
3. `git add vts/__init__.py && git commit -m "chore: deploy X.Y.0"`
4. `git push`
5. `./build.sh` (build + push images)
6. `./deploy.sh` (ssh, pull images, restart systemd services)

`deploy.sh` uses:

- `SSH_HOST`, `SSH_USER`, `REMOTE_DIR`
- `WEBAPI_SERVICE`, `WORKER_SERVICE`

Primary setup instructions:

- `docs/INITIAL_DEPLOYMENT.md`

## systemd + podman

Templates:

- `systemd/vts-webapi.service`
- `systemd/vts-worker.service`
- `systemd/vts.env.example`

Install on server:

1. Copy service files to `/etc/systemd/system/`
2. Copy env to `/etc/vts/vts.env`
3. `systemctl daemon-reload`
4. `systemctl enable --now vts-webapi vts-worker`

## Config files in repository

- `.env.example`
- `config.yaml` (example runtime config)
- `prompts/segment_prompt.md`
- `prompts/global_prompt.md`
- `PROJECT_RULES.md`





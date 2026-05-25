# Initial Deployment Guide

This guide describes first-time production deployment of `vts` on a clean Linux server with `podman` and `systemd`.

## 1. Server prerequisites

Install:

- `git`
- `podman`
- `podman-compose` (or `podman compose` plugin)
- `python3`
- `systemd`

Create directories:

```bash
sudo mkdir -p /opt/vts /opt/vts/config /opt/vts/state /srv/vts-data /opt/vts/prompts
sudo chown -R "$USER":"$USER" /opt/vts /srv/vts-data
sudo chmod 700 /opt/vts/state
```

`/opt/vts/state/` holds container-managed secrets the operator should
not edit by hand (HMAC key for the session cookie, auto-generated on
first start). It is mounted read-write into the webapi container; back
it up alongside `/opt/vts/config/vts.env`. Deleting
`/opt/vts/state/session_secret` and restarting the webapi logs out all
users (intended rotation path).

## 1.1 External AI services (required)

Deploy these services separately (they are not built in this repository):

- Whisper ASR webservice
  - image: `ghcr.io/ahmetoner/whisper-asr-webservice:latest`
  - docs: `https://github.com/ahmetoner/whisper-asr-webservice`
- llama.cpp OpenAI-compatible server
  - image: `ghcr.io/ggerganov/llama.cpp:server`
  - docs: `https://github.com/ggerganov/llama.cpp/tree/master/examples/server`

`vts` worker calls llama via OpenAI-compatible API and includes a `prepare_llama_model` DAG step to warm up the configured model before summarization.

## 2. Clone and prepare production config

```bash
cd /opt/vts
git clone <YOUR_GITHUB_REPO_URL> .
cp config.yaml /opt/vts/config/config.yaml
cp systemd/vts.env.example /opt/vts/config/vts.env
```

Edit:

- `/opt/vts/config/config.yaml` as the primary runtime config
- `/opt/vts/prompts/*.md` as prompt sources
- `/opt/vts/config/vts.env` only for container image tags and optional explicit overrides

Production note:

- `.env` / `.env.example` are for local `docker/podman compose` usage only and are not required by systemd deployment.

## 3. Bring up Postgres/Redis and initialize DB

```bash
cd /opt/vts
podman compose up -d postgres redis
CONTAINER_ENGINE=podman ./scripts/setup_postgres.sh
```

`scripts/setup_postgres.sh` is idempotent and creates/updates role/database (defaults: `vts` / `vts`).

## 4. Image source

Published tags (replace `OWNER` with your GitHub user/org or container registry namespace):

- `ghcr.io/OWNER/vts:<version>`
- `ghcr.io/OWNER/vts:latest`

Set `/opt/vts/config/vts.env`:

```bash
VTS_IMAGE=ghcr.io/OWNER/vts:<version>
```

If you need to rebuild and push images from a build host:

```bash
python -m pytest -q tests
docker login ghcr.io
export CONTAINER_ENGINE=docker
export IMAGE_REPO=ghcr.io/OWNER/vts
export USE_BUILDX=auto
export BUILDX_CACHE_REPO=ghcr.io/OWNER/vts
export BUILDX_CACHE_MODE=max
export APT_MIRROR=http://deb.debian.org/debian
export APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
./build.sh
```

Windows build host note:

- prefer running this from WSL2 with repository located in Linux filesystem (`/home/<user>/...`) for faster Docker build I/O.

If local host cannot run tests because of platform-specific dependency builds (for example Windows + `asyncpg`), run checks inside Linux container:

```bash
docker compose run --rm -v "$(pwd)":/app webapi sh -lc "pip install pytest==8.4.2 && python -m pytest -q tests"
```

## 5. DB migrations on webapi startup

`webapi` service uses the common image with `VTS_ROLE=webapi` and runs `alembic upgrade head` before starting `uvicorn`.
No separate migration container is required.

## 6. Install and start systemd units

```bash
sudo cp systemd/vts-webapi.service /etc/systemd/system/
sudo cp systemd/vts-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vts-webapi.service
sudo systemctl enable --now vts-worker.service
```

## 7. Verify deployment

```bash
sudo systemctl status vts-webapi.service --no-pager
sudo systemctl status vts-worker.service --no-pager
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/version
```

Open UI via reverse proxy that injects `X-Forwarded-User`.

## 8. Next releases

For commit/deploy rules and release sequence, follow:

- `PROJECT_RULES.md`
- `docs/ARCHITECTURE.md` (build, release, and configuration reference)

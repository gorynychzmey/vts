# Initial Deployment Guide

This guide describes first-time production deployment of `vts` on a clean Linux server with `podman` and `systemd`.

## 1. Server prerequisites

Install:

- `git`
- `podman`
- `podman-compose` (or `podman compose` plugin)
- `python3` (for local helper scripts if needed)
- `systemd`

Create directories:

```bash
sudo mkdir -p /opt/vts /etc/vts /srv/vts-data /opt/vts/prompts
sudo chown -R "$USER":"$USER" /opt/vts /srv/vts-data
```

## 2. Clone and prepare repository

```bash
cd /opt/vts
git clone <YOUR_GITHUB_REPO_URL> .
cp .env.example .env
cp systemd/vts.env.example /etc/vts/vts.env
```

Edit `.env` and `/etc/vts/vts.env`:

- `VTS_DATABASE_URL`
- `VTS_REDIS_URL`
- `VTS_WHISPER_URL`
- `VTS_LLAMA_URL`
- `VTS_ADMIN_EMAILS`
- trusted proxy CIDRs

Edit `config.yaml` and `prompts/*.md` if needed.

## 3. Build and run stack for first migration

```bash
cd /opt/vts
podman compose up -d --build postgres redis
```

Run migrations:

```bash
podman compose run --rm webapi alembic upgrade head
```

## 4. Build and publish application images

On build host:

```bash
export REGISTRY=docker.io
export NAMESPACE=<your_dockerhub_namespace>
./build.sh
```

This builds and pushes:

- `vts-webapi:<version>` and `:latest`
- `vts-worker:<version>` and `:latest`

## 5. Configure systemd units

Copy unit files:

```bash
sudo cp systemd/vts-webapi.service /etc/systemd/system/
sudo cp systemd/vts-worker.service /etc/systemd/system/
```

Update image tags in `/etc/vts/vts.env`:

```bash
WEBAPI_IMAGE=docker.io/<your_dockerhub_namespace>/vts-webapi:<version>
WORKER_IMAGE=docker.io/<your_dockerhub_namespace>/vts-worker:<version>
```

Reload and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vts-webapi.service
sudo systemctl enable --now vts-worker.service
```

## 6. Verify deployment

Check service state:

```bash
sudo systemctl status vts-webapi.service --no-pager
sudo systemctl status vts-worker.service --no-pager
```

Health/version check:

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/version
```

Open UI and create a test task through reverse proxy that sets `X-Forwarded-User`.

## 7. First deployment update flow (next releases)

Always before deployment:

1. bump MINOR (`python scripts/bump_version.py minor`)
2. run tests
3. commit and push bump
4. `./build.sh`
5. `./deploy.sh`


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

- set image tags (`WEBAPI_IMAGE`, `WORKER_IMAGE`) in `/etc/vts/vts.env`
- keep `VTS_*` values in `/etc/vts/vts.env` commented by default (use only as explicit overrides)

Edit `config.yaml` and `prompts/*.md` if needed.

## 3. Prepare Postgres and run migrations

```bash
cd /opt/vts
podman compose up -d postgres redis
CONTAINER_ENGINE=podman ./scripts/setup_postgres.sh
```

The script creates/updates role and database (defaults: `vts`/`vts`) in idempotent mode.

Run migrations after DB prep:

```bash
podman compose run --rm webapi alembic upgrade head
```

## 4. Image source (Docker Hub `gorynychzmey/vts`)

Application images are stored in a single repo:

- `docker.io/gorynychzmey/vts:<version>-webapi`
- `docker.io/gorynychzmey/vts:<version>-worker`
- `docker.io/gorynychzmey/vts:latest-webapi`
- `docker.io/gorynychzmey/vts:latest-worker`

If you need to rebuild and push from build host:

```bash
export CONTAINER_ENGINE=docker
export IMAGE_REPO=docker.io/gorynychzmey/vts
# Optional: use a Germany mirror for faster apt downloads in/near Munich
export APT_MIRROR=http://ftp.de.debian.org/debian
# Keep security updates on official mirror
export APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
./build.sh
```

## 5. Configure systemd units

Copy unit files:

```bash
sudo cp systemd/vts-webapi.service /etc/systemd/system/
sudo cp systemd/vts-worker.service /etc/systemd/system/
```

Update image tags in `/etc/vts/vts.env`:

```bash
WEBAPI_IMAGE=docker.io/gorynychzmey/vts:<version>-webapi
WORKER_IMAGE=docker.io/gorynychzmey/vts:<version>-worker
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

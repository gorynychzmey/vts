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

`scripts/setup_postgres.sh` is idempotent and creates/updates role/database (defaults: `vts` / `vts`),
and installs the `vector` extension.

The Postgres image must ship pgvector — the deployment uses
`tensorchord/vchord-postgres`, which bundles both `vector` and `vchord`. A stock
`postgres:17` image does not have it and the extension step will fail.

**The `vector` extension must be created by a superuser.** Migration
`0014_pgvector_extension` runs `CREATE EXTENSION IF NOT EXISTS vector`, but it
connects as the application role (`vts`), which is not a superuser. If the
extension is missing, every startup fails with:

```
asyncpg.exceptions.InsufficientPrivilegeError: permission denied to create extension "vector"
HINT:  Must be superuser to create this extension.
```

The webapi then crash-loops and a reverse proxy in front of it (Traefik) returns
`502 Bad gateway`, since there is no healthy backend.

`setup_postgres.sh` avoids this by creating the extension as the admin user, so
the migration finds it already present. If you provision the database by other
means (managed Postgres, an existing server, a shared instance), run this once
as a superuser before the first start:

```bash
psql -U postgres -d vts -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

Verify with `\dx` — `vector` should be listed alongside `plpgsql`.

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

Migrations run as the application role (`vts`), not as a superuser, so anything
needing superuser rights must be done out of band — see section 8 for the
versions where that applies. A failing migration aborts startup, and systemd
restarts the unit, so a migration that cannot succeed turns into a crash loop
(and `502 Bad gateway` behind a reverse proxy) rather than a one-off error.

## 6. Install and start systemd units

```bash
sudo cp systemd/vts-webapi.service /etc/systemd/system/
sudo cp systemd/vts-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vts-webapi.service
sudo systemctl enable --now vts-worker.service
```

### Diarization sidecar (optional)

Only needed if you use the `diarize` option. It is released on its own
`diar-build-X.Y.Z` tag, independently of VTS, so install it whenever you first
need it:

```bash
sudo cp systemd/vts-diarization.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vts-diarization.service
```

Requires `DIARIZATION_IMAGE` in `/opt/vts/config/vts.env` (see
`systemd/vts.env.example`). `deploy-after-diarization.yml` pulls that exact
reference and restarts this unit, so the unit must exist before the first
diarization deploy — otherwise the deploy fails fast on a missing unit.

**Speaker registry requires sidecar 1.1.0+.** The voice registry (matching
a diarized fragment to a known speaker) calls the sidecar's `POST /embed`
endpoint and reads `embedding_model` off its responses; both were added in
`diar-build-1.1.0`. Against an older sidecar, matching has no signal to
work with — deploy at least `diar-build-1.1.0` before relying on the
speaker registry in production.

## 7. Verify deployment

```bash
sudo systemctl status vts-webapi.service --no-pager
sudo systemctl status vts-worker.service --no-pager
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/api/version
```

If you installed the diarization sidecar:

```bash
sudo systemctl status vts-diarization.service --no-pager
curl -fsS http://127.0.0.1:9100/health
```

Open UI via reverse proxy that injects `X-Forwarded-User`.

## 8. Upgrade notes (manual steps)

Most releases need nothing beyond a new image tag — migrations run at startup.
The versions below are exceptions that require a manual step first.

### 1.3 → 1.4 — pgvector extension (speaker registry)

1.4 introduces the speaker registry (`vts-80i`), which stores voice embeddings
in `vector` columns. Migration `0014_pgvector_extension` enables the extension,
but it runs as the application role (`vts`), which is not a superuser, so on an
existing 1.3 database it fails and **the webapi crash-loops on every start**:

```
asyncpg.exceptions.InsufficientPrivilegeError: permission denied to create extension "vector"
HINT:  Must be superuser to create this extension.
```

While it crash-loops there is no healthy backend, so Traefik answers
`502 Bad gateway` for every request.

**Before deploying 1.4, run once as a superuser against the vts database:**

```bash
# Containerised Postgres (peer auth inside the container):
podman exec -u postgres <postgres-container> psql -d vts \
  -c 'CREATE EXTENSION IF NOT EXISTS vector'

# Or over the network as a superuser role:
psql -h <db-host> -U postgres -d vts -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

Verify, then deploy — the migration becomes a no-op via `IF NOT EXISTS`:

```bash
psql -h <db-host> -U vts -d vts -c '\dx'   # expect: vector
```

Two related prerequisites:

- The Postgres image must ship pgvector (`tensorchord/vchord-postgres`); a
  stock `postgres:17` cannot install it at all.
- The diarization sidecar must be `diar-build-1.1.0`+ (see section 6).

Fresh installs are not affected: `scripts/setup_postgres.sh` already creates the
extension as the admin user.

If the services are already crash-looping, create the extension and restart:

```bash
sudo systemctl restart vts-webapi.service vts-worker.service
```

## 9. Next releases

For commit/deploy rules and release sequence, follow:

- `PROJECT_RULES.md`
- `docs/ARCHITECTURE.md` (build, release, and configuration reference)

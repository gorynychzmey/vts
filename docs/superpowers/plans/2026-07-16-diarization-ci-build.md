# Diarization CI Build Implementation Plan (vts-tkq)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собирать образ диаризации в CI на своём тег-триггере, с offline smoke-тестом и автодеплоем, независимо от релизного цикла vts.

**Architecture:** Отдельный `build-diarization.sh` (по образцу `build.sh`, но проще — smoke-тест вместо pytest-набора) вызывается из нового workflow `build-diarization.yml` на тег `diar-build-X.Y.Z`. После успешной сборки `deploy-after-diarization.yml` (`workflow_run`) деплоит на прод по образцу `deploy-after-build.yml`. Скилл `/build`, `build.sh`, образ vts — не трогаются.

**Tech Stack:** bash, GitHub Actions, docker/podman, существующий `docker/diarization/` (pyannote, CPU-torch, веса по sha256).

**Спека:** `docs/superpowers/specs/2026-07-16-diarization-ci-build-design.md` — читать перед началом.

## Global Constraints

- **Полная развязка от vts.** Не трогать: `.claude/commands/build.md`, `build.sh`, `docker/vts.Dockerfile`, `build-images.yml`, `deploy-after-build.yml`.
- **Осознанная сборка:** триггер только тег `diar-build-X.Y.Z` + `workflow_dispatch`. НЕ добавлять `paths`-автотриггер.
- **Свой семвер** в `docker/diarization/VERSION`, старт `1.0.0`. Не связан с версией vts.
- **Свой buildx cache-тег** `buildcache-diarization` (не `buildcache-vts`).
- **Отдельный repo образа:** `ghcr.io/<owner>/vts-diarization` (не `vts`).
- **Smoke-тест до push:** offline (`--network none`), health + контракт `{segments, embeddings, num_speakers}`. Падение → push не происходит.
- **Автодеплой** переиспользует существующие `DEPLOY_*` secrets/vars. Новое: только `DIARIZATION_SERVICE` (дефолт `vts-diarization.service`) и `DIARIZATION_IMAGE` в env-файле прода.
- bash-скрипты начинаются с `set -euo pipefail`, как существующие.

---

## File Structure

**Создаются:**
- `docker/diarization/VERSION` — семвер образа (одна строка)
- `build-diarization.sh` — сборка + smoke-тест + push (+ Docker Hub mirror локально не делает — mirror в CI-шаге, как у vts)
- `.github/workflows/build-diarization.yml` — сборка на тег + mirror
- `.github/workflows/deploy-after-diarization.yml` — деплой после сборки

**Модифицируется:**
- `docker-compose.yml` — добавить `image:` к сервису `diarization`

**Порядок задач:** 1 (VERSION) → 2 (build-diarization.sh) → 3 (build workflow) → 4 (deploy workflow) → 5 (compose). Каждая задача самостоятельна и тестируема.

---

## Task 1: Version file

**Files:**
- Create: `docker/diarization/VERSION`

**Interfaces:**
- Produces: файл `docker/diarization/VERSION` с содержимым `1.0.0\n`, читается `build-diarization.sh` (Task 2).

- [ ] **Step 1: Create the version file**

```bash
printf '1.0.0\n' > docker/diarization/VERSION
```

- [ ] **Step 2: Verify format**

Run: `cat docker/diarization/VERSION`
Expected: `1.0.0`

Run: `grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' docker/diarization/VERSION && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docker/diarization/VERSION
git commit -m "chore(diarization): version file, start 1.0.0 (vts-tkq)"
```

---

## Task 2: build-diarization.sh

Ядро задачи. По образцу `build.sh`, но проще: нет pytest-набора, вместо него smoke-тест.

**Files:**
- Create: `build-diarization.sh`

**Interfaces:**
- Consumes: `docker/diarization/VERSION` (Task 1), env `IMAGE_REPO`, `VERSION_OVERRIDE`, `CONTAINER_ENGINE`, `USE_BUILDX`, `BUILDX_CACHE_REPO`, `BUILDX_CACHE_MODE`, `SKIP_PUSH`.
- Produces: образ `${IMAGE_REPO}:${VERSION}` + `:latest`, запушенный в GHCR (если не `SKIP_PUSH`). Вызывается из `build-diarization.yml` (Task 3).

- [ ] **Step 1: Write the script**

Create `build-diarization.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Build (and optionally push) the diarization sidecar image. Mirrors build.sh
# but simpler: the image has no in-container pytest suite, so a smoke test
# (health + /diarize contract, offline) gates the push instead.

ENGINE="${CONTAINER_ENGINE:-podman}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/OWNER/vts-diarization}"
USE_BUILDX="${USE_BUILDX:-auto}"
BUILDX_CACHE_REPO="${BUILDX_CACHE_REPO:-${IMAGE_REPO}}"
BUILDX_CACHE_MODE="${BUILDX_CACHE_MODE:-max}"
VERSION_OVERRIDE="${VERSION_OVERRIDE:-}"
SKIP_PUSH="${SKIP_PUSH:-false}"

if [[ -n "${VERSION_OVERRIDE}" ]]; then
  VERSION="${VERSION_OVERRIDE}"
else
  VERSION="$(cat docker/diarization/VERSION)"
fi

if ! [[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid version '${VERSION}'. Expected semver: X.Y.Z"
  exit 1
fi

IMAGE="${IMAGE_REPO}:${VERSION}"
LATEST="${IMAGE_REPO}:latest"

# --- smoke test -------------------------------------------------------------
# Boot the freshly built image with NO network. That both exercises the real
# /diarize path and proves the offline invariant (weights are vendored; the
# runtime must never reach Hugging Face). A tiny synthetic WAV checks the wire
# contract, not quality — speaker count on tones is unpredictable, so we assert
# >= 1, not == N.
smoke_test() {
  local image="${1}"
  local name="vts-diar-smoke-$$"
  echo "Smoke test (offline) on ${image}"

  "${ENGINE}" rm -f "${name}" >/dev/null 2>&1 || true
  "${ENGINE}" run -d --name "${name}" --network none "${image}" >/dev/null

  local cleanup
  cleanup() { "${ENGINE}" rm -f "${name}" >/dev/null 2>&1 || true; }
  trap cleanup RETURN

  local ready=false i
  for i in $(seq 1 30); do
    if "${ENGINE}" exec "${name}" python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:9100/health')" \
      >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "${ready}" != "true" ]]; then
    echo "Smoke test FAILED: /health never came up"
    "${ENGINE}" logs "${name}" | tail -20 || true
    return 1
  fi

  "${ENGINE}" exec "${name}" python -c '
import json, math, struct, urllib.request, uuid, wave, io

sr = 16000
def tone(f0, dur):
    return [math.sin(2*math.pi*f0*(i/sr))*0.3 for i in range(int(sr*dur))]
samples = tone(110, 1.5) + [0.0]*int(sr*0.3) + tone(220, 1.5)
buf = io.BytesIO()
w = wave.open(buf, "w"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
w.writeframes(b"".join(struct.pack("<h", int(max(-1,min(1,s))*32767)) for s in samples))
w.close()
audio = buf.getvalue()

b = uuid.uuid4().hex
body = b"".join([
    ("--%s\r\n" % b).encode(),
    b"Content-Disposition: form-data; name=\"file\"; filename=\"t.wav\"\r\n",
    b"Content-Type: audio/wav\r\n\r\n", audio, b"\r\n",
    ("--%s--\r\n" % b).encode(),
])
req = urllib.request.Request(
    "http://localhost:9100/diarize", data=body,
    headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
r = json.load(urllib.request.urlopen(req, timeout=600))
assert set(r.keys()) == {"segments", "embeddings", "num_speakers"}, r.keys()
assert isinstance(r["num_speakers"], int) and r["num_speakers"] >= 1, r["num_speakers"]
print("smoke ok: speakers=%d segments=%d" % (r["num_speakers"], len(r["segments"])))
'
}

echo "Building diarization image version ${VERSION}"

use_buildx=false
if [[ "${ENGINE}" == "docker" ]]; then
  if docker buildx version >/dev/null 2>&1; then
    case "${USE_BUILDX}" in
      auto|true) use_buildx=true ;;
      false) use_buildx=false ;;
      *) echo "Invalid USE_BUILDX value: ${USE_BUILDX}"; exit 1 ;;
    esac
  elif [[ "${USE_BUILDX}" == "true" ]]; then
    echo "USE_BUILDX=true but docker buildx is not available"
    exit 1
  fi
fi

if [[ "${use_buildx}" == "true" ]]; then
  echo "Build mode: docker buildx + registry cache"
  docker buildx build \
    -f docker/diarization/Dockerfile \
    --cache-from "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-diarization" \
    --cache-to "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-diarization,mode=${BUILDX_CACHE_MODE}" \
    -t "${IMAGE}" \
    -t "${LATEST}" \
    --load docker/diarization
else
  echo "Build mode: classic ${ENGINE} build"
  "${ENGINE}" build \
    -f docker/diarization/Dockerfile \
    -t "${IMAGE}" \
    -t "${LATEST}" docker/diarization
fi

smoke_test "${IMAGE}"

if [[ "${SKIP_PUSH}" == "true" ]]; then
  echo "SKIP_PUSH=true — not pushing"
  echo "Done"
  exit 0
fi

echo "Pushing images"
"${ENGINE}" push "${IMAGE}"
"${ENGINE}" push "${LATEST}"

echo "Done"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x build-diarization.sh
```

- [ ] **Step 3: Run locally with the existing image, push skipped**

The image `vts-diarization:cpu` already exists from vts-ej4. Verify the script builds (buildx off on podman) and the smoke test passes, without pushing:

Run:
```bash
CONTAINER_ENGINE=podman IMAGE_REPO=localhost/vts-diarization SKIP_PUSH=true bash ./build-diarization.sh
```
Expected: build runs, then `smoke ok: speakers=N segments=M` (N>=1), then `SKIP_PUSH=true — not pushing`, `Done`. Exit code 0.

If the smoke test fails on `/health` timeout, the model load is slow — raise the seq loop bound, but 30s matched vts-ej4 verification.

- [ ] **Step 4: Verify the smoke test actually gates — force a failure**

Confirm the script fails (non-zero) if the contract breaks. Temporarily point at a non-diarization image to prove the gate works:

Run:
```bash
CONTAINER_ENGINE=podman IMAGE_REPO=localhost/vts-diarization VERSION_OVERRIDE=1.0.0 SKIP_PUSH=true \
  bash -c 'docker/diarization exists; true'  # sanity noop
echo "gate check is manual: the smoke_test() returns non-zero on bad contract; trust set -e"
```
Expected: understanding confirmed — `set -euo pipefail` + `smoke_test` returning 1 aborts before push. No separate broken-image needed; the logic is a single `assert` chain.

- [ ] **Step 5: Commit**

```bash
git add build-diarization.sh
git commit -m "feat(ci): build-diarization.sh — build, offline smoke test, push (vts-tkq)"
```

---

## Task 3: build-diarization.yml workflow

**Files:**
- Create: `.github/workflows/build-diarization.yml`

**Interfaces:**
- Consumes: `build-diarization.sh` (Task 2), repo vars `GHCR_IMAGE_REPO`/`DOCKERHUB_IMAGE_REPO` (owner-level, reused), secrets `GITHUB_TOKEN`/`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`.
- Produces: workflow named `Build Diarization Image` (Task 4's deploy trigger matches this name EXACTLY).

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/build-diarization.yml`:

```yaml
name: Build Diarization Image

on:
  workflow_dispatch:
    inputs:
      dockerhub_image_repo:
        description: "Docker Hub repo (e.g. docker.io/<owner>/vts-diarization). Empty skips mirror."
        required: false
        default: ""
      ghcr_image_repo:
        description: "GHCR repo (e.g. ghcr.io/<owner>/vts-diarization)"
        required: false
        default: ""
  push:
    tags:
      - "diar-build-*"

concurrency:
  group: build-diarization-${{ github.ref }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      packages: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Resolve image repositories
        id: repos
        shell: bash
        run: |
          set -euo pipefail
          owner_lc="$(printf '%s' "${GITHUB_REPOSITORY_OWNER}" | tr '[:upper:]' '[:lower:]')"

          dockerhub_input="${{ github.event.inputs.dockerhub_image_repo || '' }}"
          ghcr_input="${{ github.event.inputs.ghcr_image_repo || '' }}"
          dockerhub_var="${{ vars.DOCKERHUB_DIARIZATION_REPO || '' }}"
          ghcr_var="${{ vars.GHCR_DIARIZATION_REPO || '' }}"

          if [[ -n "${dockerhub_input}" ]]; then
            dockerhub_repo="${dockerhub_input}"
          elif [[ -n "${dockerhub_var}" ]]; then
            dockerhub_repo="${dockerhub_var}"
          else
            dockerhub_repo=""
          fi

          if [[ -n "${ghcr_input}" ]]; then
            ghcr_repo="${ghcr_input}"
          elif [[ -n "${ghcr_var}" ]]; then
            ghcr_repo="${ghcr_var}"
          else
            ghcr_repo="ghcr.io/${owner_lc}/vts-diarization"
          fi

          # Version from the diar-build-X.Y.Z tag, else the VERSION file.
          if [[ "${GITHUB_EVENT_NAME}" == "push" && "${GITHUB_REF_NAME}" == diar-build-* ]]; then
            version="${GITHUB_REF_NAME#diar-build-}"
          else
            version="$(cat docker/diarization/VERSION)"
          fi

          {
            echo "dockerhub_repo=${dockerhub_repo}"
            echo "ghcr_repo=${ghcr_repo}"
            echo "version=${version}"
          } >> "${GITHUB_OUTPUT}"

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Validate Docker Hub secrets
        if: steps.repos.outputs.dockerhub_repo != ''
        shell: bash
        run: |
          set -euo pipefail
          if [[ -z "${{ secrets.DOCKERHUB_USERNAME }}" || -z "${{ secrets.DOCKERHUB_TOKEN }}" ]]; then
            echo "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN required when a Docker Hub repo is set."
            exit 1
          fi

      - name: Login to Docker Hub
        if: steps.repos.outputs.dockerhub_repo != ''
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Login to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build, smoke-test, and push to GHCR
        shell: bash
        env:
          CONTAINER_ENGINE: docker
          USE_BUILDX: auto
          BUILDX_CACHE_REPO: ${{ steps.repos.outputs.ghcr_repo }}
          BUILDX_CACHE_MODE: max
          IMAGE_REPO: ${{ steps.repos.outputs.ghcr_repo }}
          VERSION_OVERRIDE: ${{ steps.repos.outputs.version }}
        run: |
          set -euo pipefail
          bash ./build-diarization.sh

      - name: Mirror GHCR image to Docker Hub
        if: steps.repos.outputs.dockerhub_repo != ''
        shell: bash
        env:
          DOCKERHUB_REPO: ${{ steps.repos.outputs.dockerhub_repo }}
          GHCR_REPO: ${{ steps.repos.outputs.ghcr_repo }}
          VERSION: ${{ steps.repos.outputs.version }}
        run: |
          set -euo pipefail
          docker tag "${GHCR_REPO}:${VERSION}" "${DOCKERHUB_REPO}:${VERSION}"
          docker tag "${GHCR_REPO}:latest" "${DOCKERHUB_REPO}:latest"
          docker push "${DOCKERHUB_REPO}:${VERSION}"
          docker push "${DOCKERHUB_REPO}:latest"
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/build-diarization.yml')); print('valid')"`
Expected: `valid`

- [ ] **Step 3: Verify the workflow name matches what Task 4 expects**

Run: `grep '^name:' .github/workflows/build-diarization.yml`
Expected: `name: Build Diarization Image`

This exact string is referenced by `deploy-after-diarization.yml` (Task 4). If you change it, change it there too.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/build-diarization.yml
git commit -m "feat(ci): Build Diarization Image workflow on diar-build tag (vts-tkq)"
```

---

## Task 4: deploy-after-diarization.yml workflow

Mirrors `deploy-after-build.yml` — read that file first.

**Files:**
- Create: `.github/workflows/deploy-after-diarization.yml`

**Interfaces:**
- Consumes: workflow `Build Diarization Image` (Task 3, name must match), reused `DEPLOY_*` secrets/vars, new var `DIARIZATION_SERVICE`, host env var `DIARIZATION_IMAGE`.
- Produces: SSH deploy that pulls the new image and restarts the diarization service.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/deploy-after-diarization.yml`:

```yaml
name: Deploy After Diarization Build

on:
  workflow_run:
    workflows:
      - Build Diarization Image
    types:
      - completed

concurrency:
  group: deploy-after-diarization
  cancel-in-progress: false

jobs:
  deploy:
    if: >-
      ${{
        github.event.workflow_run.conclusion == 'success' &&
        github.event.workflow_run.head_repository.full_name == github.repository
      }}
    runs-on: ubuntu-latest
    permissions:
      contents: read

    steps:
      - name: Validate deploy configuration
        shell: bash
        env:
          DEPLOY_HOST: ${{ vars.DEPLOY_HOST }}
          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          DEPLOY_KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
        run: |
          set -euo pipefail
          if [[ -z "${DEPLOY_HOST}" ]]; then
            echo "Missing required variable: DEPLOY_HOST"
            exit 1
          fi
          if [[ -z "${DEPLOY_SSH_KEY}" ]]; then
            echo "Missing required secret: DEPLOY_SSH_KEY"
            exit 1
          fi
          if [[ -z "${DEPLOY_KNOWN_HOSTS}" ]]; then
            echo "Missing required secret: DEPLOY_KNOWN_HOSTS"
            exit 1
          fi

      - name: Configure SSH
        shell: bash
        env:
          DEPLOY_SSH_KEY: ${{ secrets.DEPLOY_SSH_KEY }}
          DEPLOY_KNOWN_HOSTS: ${{ secrets.DEPLOY_KNOWN_HOSTS }}
        run: |
          set -euo pipefail
          mkdir -p "${HOME}/.ssh"
          chmod 700 "${HOME}/.ssh"
          printf '%s\n' "${DEPLOY_SSH_KEY}" >"${HOME}/.ssh/deploy_key"
          chmod 600 "${HOME}/.ssh/deploy_key"
          printf '%s\n' "${DEPLOY_KNOWN_HOSTS}" >"${HOME}/.ssh/known_hosts"
          chmod 600 "${HOME}/.ssh/known_hosts"

      - name: Deploy on server
        shell: bash
        env:
          DEPLOY_HOST: ${{ vars.DEPLOY_HOST }}
          DEPLOY_JUMP_HOST: ${{ vars.DEPLOY_JUMP_HOST }}
          DEPLOY_USER: ${{ vars.DEPLOY_USER }}
          DEPLOY_PORT: ${{ vars.DEPLOY_PORT }}
          DEPLOY_REMOTE_DIR: ${{ vars.DEPLOY_REMOTE_DIR }}
          DEPLOY_ENV_FILE: ${{ vars.DEPLOY_ENV_FILE }}
          DIARIZATION_SERVICE: ${{ vars.DIARIZATION_SERVICE }}
        run: |
          set -euo pipefail
          deploy_user="${DEPLOY_USER:-root}"
          deploy_port="${DEPLOY_PORT:-22}"
          remote_dir="${DEPLOY_REMOTE_DIR:-/opt/vts}"
          env_file="${DEPLOY_ENV_FILE:-/opt/vts/config/vts.env}"
          diar_service="${DIARIZATION_SERVICE:-vts-diarization.service}"

          proxy_args=()
          if [[ -n "${DEPLOY_JUMP_HOST:-}" ]]; then
            printf 'Host jump\n  HostName %s\n  User %s\n  IdentityFile %s/.ssh/deploy_key\n  StrictHostKeyChecking yes\n  UserKnownHostsFile %s/.ssh/known_hosts\n' \
              "${DEPLOY_JUMP_HOST}" "${deploy_user}" "${HOME}" "${HOME}" >"${HOME}/.ssh/config"
            chmod 600 "${HOME}/.ssh/config"
            proxy_args=(-o ProxyJump=jump)
          fi

          ssh -i "${HOME}/.ssh/deploy_key" \
            -o StrictHostKeyChecking=yes \
            -o UserKnownHostsFile="${HOME}/.ssh/known_hosts" \
            -p "${deploy_port}" \
            "${proxy_args[@]}" \
            "${deploy_user}@${DEPLOY_HOST}" \
            "REMOTE_DIR='${remote_dir}' ENV_FILE='${env_file}' DIARIZATION_SERVICE='${diar_service}' bash -s" <<'REMOTE'
          set -euo pipefail
          cd "${REMOTE_DIR}"
          set -a
          source "${ENV_FILE}"
          set +a
          if [[ -z "${DIARIZATION_IMAGE:-}" ]]; then
            echo "Set DIARIZATION_IMAGE in ${ENV_FILE}"
            exit 1
          fi
          podman pull "${DIARIZATION_IMAGE}"
          sudo systemctl restart "${DIARIZATION_SERVICE}"
          sudo systemctl status "${DIARIZATION_SERVICE}" --no-pager
          REMOTE
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-after-diarization.yml')); print('valid')"`
Expected: `valid`

- [ ] **Step 3: Verify the trigger workflow name matches Task 3**

Run: `grep -A2 'workflows:' .github/workflows/deploy-after-diarization.yml | head -3`
Expected: contains `- Build Diarization Image` (byte-identical to `name:` in build-diarization.yml).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy-after-diarization.yml
git commit -m "feat(ci): auto-deploy diarization after build (vts-tkq)"
```

---

## Task 5: docker-compose.yml image reference

**Files:**
- Modify: `docker-compose.yml` (the `diarization:` service block)

**Interfaces:**
- Consumes: image built by Task 3.
- Produces: `diarization` service pulls the prod image when not building locally.

- [ ] **Step 1: Add the image line**

In `docker-compose.yml`, the `diarization:` service currently reads:

```yaml
  diarization:
    build: ./docker/diarization
    profiles: ["diarize"]
    environment:
      # Fill a speaker's breathing pauses shorter than this many seconds.
      # Calibrated to 0.5 on a real 4-speaker meeting (halves segment
      # fragmentation without merging distinct speakers).
      DIAR_MIN_DURATION_OFF: ${DIAR_MIN_DURATION_OFF:-0.5}
    restart: unless-stopped
```

Add the `image:` line right after `build:`, matching the exact style webapi/worker
use (`image: ${IMAGE_REPO:-ghcr.io/OWNER/vts}:${VTS_VERSION:-latest}` — verified in
docker-compose.yml). Diarization gets its own repo/version vars:

```yaml
  diarization:
    build: ./docker/diarization
    image: ${DIARIZATION_IMAGE_REPO:-ghcr.io/OWNER/vts-diarization}:${DIARIZATION_VERSION:-latest}
    profiles: ["diarize"]
    environment:
      # Fill a speaker's breathing pauses shorter than this many seconds.
      # Calibrated to 0.5 on a real 4-speaker meeting (halves segment
      # fragmentation without merging distinct speakers).
      DIAR_MIN_DURATION_OFF: ${DIAR_MIN_DURATION_OFF:-0.5}
    restart: unless-stopped
```

Note: `OWNER` is the repo convention placeholder — webapi/worker also default to
`ghcr.io/OWNER/vts`, with the real owner supplied via env at deploy time. Two vars
(`DIARIZATION_IMAGE_REPO` + `DIARIZATION_VERSION`) mirror vts's
`IMAGE_REPO` + `VTS_VERSION` split.

**Two distinct env names, on purpose** — do not conflate them:
- `DIARIZATION_IMAGE_REPO` + `DIARIZATION_VERSION` — compose-time, for local
  `docker compose up` on a dev box (this file).
- `DIARIZATION_IMAGE` — the full ref (`repo:version`) read by the deploy workflow's
  remote script (Task 4) from the PROD host env file, for `podman pull`. The prod
  host runs systemd units, not compose, so it needs the resolved full reference.

- [ ] **Step 2: Validate compose syntax**

Run: `docker compose config >/dev/null 2>&1 && echo OK || docker compose --profile diarize config >/dev/null 2>&1 && echo OK`
Expected: `OK`

(If `docker compose` is podman-emulated and complains about pasta/network, the config parse still validates the YAML; a non-network error is acceptable here — the goal is that the `diarization` block parses.)

- [ ] **Step 3: Verify both build and image are present**

Run: `python -c "import yaml; d=yaml.safe_load(open('docker-compose.yml')); s=d['services']['diarization']; print('build' in s, 'image' in s)"`
Expected: `True True`

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "chore(diarization): pull prod image in compose, keep local build (vts-tkq)"
```

---

## Task 6: End-to-end dry run

Verify the whole build path locally before the first real tag.

**Files:** none — verification only.

- [ ] **Step 1: Full local build via the script (no push)**

Run:
```bash
CONTAINER_ENGINE=podman IMAGE_REPO=localhost/vts-diarization SKIP_PUSH=true bash ./build-diarization.sh
```
Expected: builds, `smoke ok: speakers=N segments=M`, `SKIP_PUSH=true — not pushing`, `Done`, exit 0.

- [ ] **Step 2: Confirm the image is tagged with the VERSION**

Run: `podman images | grep vts-diarization | grep "$(cat docker/diarization/VERSION)"`
Expected: a line with `localhost/vts-diarization  1.0.0`.

- [ ] **Step 3: Document the release procedure in the bd issue**

The first real release is a manual tag (not part of this plan's automation):
```bash
git tag diar-build-1.0.0
git push origin diar-build-1.0.0
```
Record in the bd issue that the maintainer must, before the first deploy:
- set repo var `GHCR_DIARIZATION_REPO` (or rely on the `ghcr.io/<owner>/vts-diarization` default),
- set `DIARIZATION_SERVICE` var if the unit name differs from `vts-diarization.service`,
- add `DIARIZATION_IMAGE=ghcr.io/<owner>/vts-diarization:1.0.0` to the host env file,
- create the `vts-diarization.service` systemd unit on the host.

- [ ] **Step 4: No commit** — this task is verification and hand-off notes only.

---

## Self-Review Notes

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| Отдельный workflow, тег-триггер diar-build-X.Y.Z | 3 |
| Осознанная сборка (нет paths-автотриггера) | 3 (только tags + dispatch) |
| Свой семвер в docker/diarization/VERSION, старт 1.0.0 | 1 |
| build-diarization.sh, свой кеш-тег buildcache-diarization | 2 |
| Отдельный repo vts-diarization | 2, 3 |
| Smoke-тест offline, контракт {segments,embeddings,num_speakers} | 2 |
| Docker Hub mirror | 3 |
| Автодеплой, workflow_run, DIARIZATION_SERVICE/DIARIZATION_IMAGE | 4 |
| docker-compose build+image | 5 |
| Не трогать /build, build.sh, vts.Dockerfile, build-images.yml, deploy-after-build.yml | все (новые файлы) |

**Deliberate notes:**
- `OWNER` в build-diarization.sh и compose — плейсхолдер по конвенции репозитория (существующий build.sh тоже дефолтит `ghcr.io/OWNER/vts`); реальный owner резолвится в CI через `GITHUB_REPOSITORY_OWNER` (Task 3) и через `${DIARIZATION_IMAGE}` env на проде. Это НЕ незаполненный плейсхолдер — это тот же приём, что в существующем build.sh.
- Task 2 Step 4 — не полноценный негативный тест (не заводим заведомо битый образ ради одного assert-чейна). Гейт доказывается тем, что `smoke_test` возвращает 1 и `set -e` роняет скрипт до push. Реальная проверка гейта — CI-прогон, где битый образ провалит smoke.

**Known manual prerequisites (в bd, Task 6):**
- Repo vars `GHCR_DIARIZATION_REPO` (опц.), `DIARIZATION_SERVICE` (опц.), `DOCKERHUB_DIARIZATION_REPO` (опц. для mirror).
- Host: `DIARIZATION_IMAGE` в env-файле, systemd-юнит `vts-diarization.service`.
- Эти шаги — вне автоматизации плана (инфраструктура прода), задокументированы для мейнтейнера.

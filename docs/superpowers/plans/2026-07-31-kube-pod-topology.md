# Kube Pod Topology Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two systemd units that run VTS (`vts-webapi.service`, `vts-worker.service`, each a bare `podman run`) with a single `podman kube play` pod driven by one `vts.service`, moving DB migrations into an initContainer and adding thin oneshot units so individual containers can still be restarted via `systemctl`.

**Architecture:** One Pod manifest (`deploy/vts.yaml`) declares an initContainer (`migrate`) plus two long-running containers (`webapi`, `worker`), all sharing the host env file and the two hostPath volumes. `vts.service` plays/downs that manifest. `vts-worker-restart.service` / `vts-webapi-restart.service` are `Type=oneshot` wrappers around `podman restart <container>`. The diarization sidecar is untouched and keeps its own unit.

**Tech Stack:** podman 5.7.0 (rootful), systemd, Kubernetes Pod YAML subset supported by `podman kube play`, existing `docker/vts-entrypoint.sh` (sh), GitHub Actions.

## Global Constraints

- **Target host is production and is the machine this runs on.** `sudo` is available. `vts-webapi` and `vts-worker` are currently serving. Every host-mutating step must be reversible and must be preceded by the state check in Task 6.
- **Rootful podman.** All `podman`/`systemctl` commands use `sudo`. Rootless podman has a separate container store and will NOT see these containers.
- **Secrets stay in `/opt/vts/config/vts.env` on the host.** Never inline secret values into `deploy/vts.yaml` (unlike `/opt/cognee/cognee.yaml`, which has them in a ConfigMap). The manifest references the env file, it does not copy it.
- **The diarization sidecar (`vts-diarization.service`) is out of scope.** Do not edit, stop, or fold it into the pod. VTS reaches it at `VTS_DIARIZATION_URL=http://vts-diarization.dns.podman:9100`.
- **Exact current values** (verified on host, use verbatim): image `ghcr.io/gorynychzmey/vts:latest` (from `VTS_IMAGE` in `vts.env`); published port mapping `8086:8080`; volumes `/opt/vts:/opt/vts` and `/disk/vts-data:/srv/vts-data`; autoupdate label value `registry`; container names `vts-webapi`, `vts-worker`.
- **Old unit files are deleted only in Task 8**, after the new topology is confirmed working. Until then they remain on disk as the rollback path.
- **Version bump:** bump `vts/__init__.py` before committing code changes (project rule). Pure-YAML/systemd tasks that do not touch `vts/` do not need a bump.

---

## File Structure

**Repo:**
- `deploy/vts.yaml` — NEW. The Pod manifest: initContainer `migrate`, containers `webapi` + `worker`, hostPath volumes, hostPort.
- `systemd/vts.service` — NEW. Plays/downs the manifest.
- `systemd/vts-worker-restart.service` — NEW. `Type=oneshot`, `podman restart vts-worker`.
- `systemd/vts-webapi-restart.service` — NEW. `Type=oneshot`, `podman restart vts-webapi`.
- `docker/vts-entrypoint.sh` — MODIFY. Add `VTS_ROLE=migrate`; `start_webapi` stops migrating.
- `.github/workflows/deploy-after-build.yml` — MODIFY. Two `systemctl restart` calls become one.
- `systemd/vts-webapi.service`, `systemd/vts-worker.service` — DELETE (Task 8 only).
- `docs/deploy-pod.md` — NEW. Operator commands (restart one container, maintenance stop, rollback).

**Host (not in repo):**
- `/opt/vts/vts.yaml` — the manifest, copied from `deploy/vts.yaml`.
- `/etc/systemd/system/vts*.service` — the units (the existing ones are symlinks into the repo; keep that convention).

---

## Task 1: Add the `migrate` role to the entrypoint

Migrations currently run inside `start_webapi`. In a pod both containers start in parallel, so the worker can begin before the schema is current. Splitting the role lets an initContainer own migrations.

**Files:**
- Modify: `docker/vts-entrypoint.sh`
- Modify: `vts/__init__.py` (version bump)

**Interfaces:**
- Produces: `VTS_ROLE=migrate` — runs preflight + `alembic upgrade head`, then exits 0. `VTS_ROLE=webapi` no longer migrates. `VTS_ROLE=both` keeps migrating (it is the single-container dev/compose path and has no initContainer in front of it).

- [ ] **Step 1: Read the current entrypoint**

Run: `cat docker/vts-entrypoint.sh`
Confirm it contains `migrate()`, `start_webapi()`, `start_worker()`, `start_both()` and a `case "${VTS_ROLE:-webapi}"`.

- [ ] **Step 2: Add the `migrate` role and drop migration from webapi**

Change `start_webapi` so it no longer calls `migrate`:

```sh
start_webapi() {
  exec uvicorn vts.api.main:app --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips "*" \
    --timeout-graceful-shutdown "${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN}"
}
```

Add a run-once role above the `case`:

```sh
# Run-once role for the pod's initContainer: apply migrations, then exit 0 so
# podman proceeds to the main containers. webapi no longer migrates, because in
# a pod it starts in parallel with worker and neither may run against an
# unmigrated schema.
run_migrate() {
  migrate
  echo "migrations applied"
}
```

Add the branch to the `case`:

```sh
  migrate)
    run_migrate
    ;;
```

and extend the error message:

```sh
    echo "Unsupported VTS_ROLE='${VTS_ROLE:-}'. Use webapi, worker, both, or migrate." >&2
```

Leave `start_both` calling `migrate` — that path has no initContainer in front of it.

- [ ] **Step 3: Verify the script is still valid sh and dispatches correctly**

Run:
```bash
sh -n docker/vts-entrypoint.sh && echo "syntax OK"
VTS_ROLE=bogus sh docker/vts-entrypoint.sh; echo "exit=$?"
```
Expected: `syntax OK`, then the usage message mentioning `migrate` and `exit=1`.

- [ ] **Step 4: Verify the migrate role reaches alembic (without a DB)**

Run:
```bash
VTS_ROLE=migrate sh -c 'set -x; . /dev/null; sh docker/vts-entrypoint.sh' 2>&1 | head -5 || true
```
Expected: it attempts `python -m vts.db.preflight` (failing on a missing DB/module outside the container is fine — the point is that the branch dispatches to migration, not to uvicorn).

- [ ] **Step 5: Bump version and commit**

```bash
python scripts/bump_version.py patch 2>/dev/null || sed -i 's/__version__ = "\(.*\)"/__version__ = "\1"/' vts/__init__.py
git add docker/vts-entrypoint.sh vts/__init__.py
git commit -m "feat(deploy): add VTS_ROLE=migrate; webapi no longer migrates (vts-0pg)"
```

---

## Task 2: Write the Pod manifest

**Files:**
- Create: `deploy/vts.yaml`

**Interfaces:**
- Produces: a Pod named `vts` whose containers are named `webapi` and `worker`. `podman kube play` derives container names as `<pod>-<container>`, i.e. **`vts-webapi`** and **`vts-worker`** — the same names the current units use, so Task 4's oneshot units and any operator muscle memory keep working.

- [ ] **Step 1: Create `deploy/vts.yaml`**

```yaml
# VTS production pod (vts-0pg). Played by systemd/vts.service via
# `podman kube play`. Host copy lives at /opt/vts/vts.yaml.
#
# Secrets are NOT in this file: every container reads /opt/vts/config/vts.env
# from the host. Do not add a ConfigMap with credentials here.
apiVersion: v1
kind: Pod
metadata:
  name: vts
  annotations:
    # Same policy the old units carried as a --label.
    io.containers.autoupdate/webapi: "registry"
    io.containers.autoupdate/worker: "registry"
spec:
  restartPolicy: Always
  # Migrations run to completion before webapi/worker start. This is the whole
  # reason the pod exists ahead of the plugin loader (vts-9y7), whose bootstrap
  # will become a second initContainer right here.
  initContainers:
    - name: migrate
      image: ghcr.io/gorynychzmey/vts:latest
      env:
        - name: VTS_ROLE
          value: migrate
      envFrom:
        - configMapRef:
            name: vts-env
      volumeMounts:
        - name: opt-vts
          mountPath: /opt/vts
        - name: vts-data
          mountPath: /srv/vts-data
  containers:
    - name: webapi
      image: ghcr.io/gorynychzmey/vts:latest
      env:
        - name: VTS_ROLE
          value: webapi
      envFrom:
        - configMapRef:
            name: vts-env
      ports:
        - containerPort: 8080
          hostPort: 8086
      volumeMounts:
        - name: opt-vts
          mountPath: /opt/vts
        - name: vts-data
          mountPath: /srv/vts-data
    - name: worker
      image: ghcr.io/gorynychzmey/vts:latest
      env:
        - name: VTS_ROLE
          value: worker
      envFrom:
        - configMapRef:
            name: vts-env
      volumeMounts:
        - name: opt-vts
          mountPath: /opt/vts
        - name: vts-data
          mountPath: /srv/vts-data
  volumes:
    - name: opt-vts
      hostPath:
        path: /opt/vts
        type: Directory
    - name: vts-data
      hostPath:
        path: /disk/vts-data
        type: Directory
```

- [ ] **Step 2: Understand how env reaches the containers (VERIFIED constraint)**

`podman kube play` on this host (5.7.0) offers **`--configmap <file>` only — there is NO `--env-file`**
(checked with `podman kube play --help`). So `envFrom.configMapRef` stays in the manifest, and the
ConfigMap must be a real file.

Putting that file in git would mean committing `VTS_OAUTH_CLIENT_SECRET`, which the spec forbids.
Resolution: **the ConfigMap is generated on the host from the existing `vts.env`** and lives beside it at
`/opt/vts/vts-configmap.yaml` (same owner/permissions). Git carries only the generator (Task 2a), never
the values. `vts.env` remains the single source of truth for secrets — the ConfigMap is a derived
artifact, regenerated whenever `vts.env` changes.

Keep the `envFrom:` blocks exactly as written above. No edit to the manifest is needed in this step.

- [ ] **Step 3: Validate the YAML parses**

Run: `python -c "import yaml,sys; d=list(yaml.safe_load_all(open('deploy/vts.yaml'))); print([x['kind'] for x in d]); print('containers:', [c['name'] for c in d[0]['spec']['containers']]); print('init:', [c['name'] for c in d[0]['spec']['initContainers']])"`
Expected: `['Pod']`, `containers: ['webapi', 'worker']`, `init: ['migrate']`.

- [ ] **Step 4: Confirm no secret values leaked into the manifest**

Run: `grep -nEi "password|secret|token|client_id|api_key" deploy/vts.yaml; echo "exit=$?"`
Expected: no matches (`exit=1`).

- [ ] **Step 5: Commit**

```bash
git add deploy/vts.yaml
git commit -m "feat(deploy): pod manifest with migrate initContainer + webapi/worker (vts-0pg)"
```

---

## Task 2a: Generator that derives the ConfigMap from `vts.env`

`kube play` can only take env via `--configmap <file>`, and that file must contain the values — including
`VTS_OAUTH_CLIENT_SECRET`. It therefore lives on the host, never in git. This script produces it.

**Files:**
- Create: `scripts/render-configmap.sh`

**Interfaces:**
- Consumes: `/opt/vts/config/vts.env` (or `$1`).
- Produces: a `kind: ConfigMap` YAML named **`vts-env`** on stdout — the exact name `envFrom.configMapRef`
  refers to in `deploy/vts.yaml`. Task 3 wires `--configmap /opt/vts/vts-configmap.yaml` into the unit;
  Task 6 runs this script to create that file.

- [ ] **Step 1: Write the generator**

```bash
#!/usr/bin/env bash
# Render a Kubernetes ConfigMap from a KEY=VALUE env file, for `podman kube play
# --configmap`. kube play has no --env-file, so this is how /opt/vts/config/vts.env
# reaches the pod (vts-0pg).
#
# The OUTPUT CONTAINS SECRETS (VTS_OAUTH_CLIENT_SECRET). Write it beside vts.env
# on the host with the same ownership/permissions — never into the repo.
#
#   sudo ./scripts/render-configmap.sh > /opt/vts/vts-configmap.yaml
set -euo pipefail

ENV_FILE="${1:-/opt/vts/config/vts.env}"
NAME="${CONFIGMAP_NAME:-vts-env}"

[ -r "$ENV_FILE" ] || { echo "cannot read $ENV_FILE" >&2; exit 1; }

printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %s\ndata:\n' "$NAME"

# Only KEY=VALUE lines; skip blanks and comments. Values are emitted as
# single-quoted YAML scalars (internal ' doubled), which keeps #, :, spaces and
# URLs literal.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  case "$line" in
    *=*) ;;
    *) continue ;;
  esac
  key=${line%%=*}
  value=${line#*=}
  case "$key" in
    [A-Za-z_][A-Za-z_0-9]*) ;;
    *) continue ;;
  esac
  # Strip one layer of surrounding quotes, as the shell would when sourcing.
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  escaped=$(printf '%s' "$value" | sed "s/'/''/g")
  printf "  %s: '%s'\n" "$key" "$escaped"
done < "$ENV_FILE"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/render-configmap.sh
```

- [ ] **Step 3: Test it on a fixture with no real secrets**

```bash
cat > /tmp/fake.env <<'EOF'
# comment line
VTS_IMAGE=ghcr.io/example/vts:latest
VTS_PUBLIC_BASE_URL=https://vts.example.com
QUOTED="value with spaces"
TRICKY=it's#literal:yes

EOF
./scripts/render-configmap.sh /tmp/fake.env
```
Expected exactly:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: vts-env
data:
  VTS_IMAGE: 'ghcr.io/example/vts:latest'
  VTS_PUBLIC_BASE_URL: 'https://vts.example.com'
  QUOTED: 'value with spaces'
  TRICKY: 'it''s#literal:yes'
```

- [ ] **Step 4: Verify the output is valid YAML and round-trips the awkward value**

```bash
./scripts/render-configmap.sh /tmp/fake.env | python -c "
import sys, yaml
d = yaml.safe_load(sys.stdin)
assert d['kind'] == 'ConfigMap', d['kind']
assert d['metadata']['name'] == 'vts-env', d['metadata']['name']
assert d['data']['TRICKY'] == \"it's#literal:yes\", d['data']['TRICKY']
assert d['data']['QUOTED'] == 'value with spaces', d['data']['QUOTED']
print('configmap render OK')"
```
Expected: `configmap render OK`.

- [ ] **Step 5: Verify it renders the REAL env file without crashing (do not print it)**

```bash
sudo ./scripts/render-configmap.sh /opt/vts/config/vts.env | python -c "
import sys, yaml
d = yaml.safe_load(sys.stdin)
print('keys rendered:', len(d['data']))
print('names:', sorted(d['data']))"
```
Expected: 6 keys, names only — **never echo the rendered file to the terminal or into a log.**

- [ ] **Step 6: Commit**

```bash
git add scripts/render-configmap.sh
git commit -m "feat(deploy): render a kube ConfigMap from vts.env (vts-0pg)"
```

---

## Task 3: Write `vts.service`

**Files:**
- Create: `systemd/vts.service`

**Interfaces:**
- Consumes: `/opt/vts/vts.yaml` (host copy of `deploy/vts.yaml`), `/opt/vts/vts-configmap.yaml` (rendered
  by Task 2a), `/opt/vts/config/vts.env` (only for `${VTS_IMAGE}` in the pull step).
- Produces: a unit that starts/stops the whole pod. Container names it yields: `vts-webapi`, `vts-worker`.

- [ ] **Step 1: Create `systemd/vts.service`**

Modelled on the working `/etc/systemd/system/cognee.service` on this host.

```ini
[Unit]
Description=VTS (podman kube pod: webapi + worker)
Documentation=man:podman-kube-play(1)
Wants=network-online.target
After=network-online.target
RequiresMountsFor=%t/containers

[Service]
Environment=PODMAN_SYSTEMD_UNIT=%n
EnvironmentFile=/opt/vts/config/vts.env
Restart=on-failure
RestartSec=5
# Migrations run in an initContainer, so first start can take a while.
TimeoutStartSec=300
# Pull before playing: the deploy flow updates the :latest tag and expects a
# restart to pick it up. kube play alone would reuse the local image.
ExecStartPre=/usr/bin/podman pull ${VTS_IMAGE}
# Re-derive the ConfigMap from vts.env on every start, so editing vts.env and
# restarting is enough — the ConfigMap can never drift from its source.
ExecStartPre=/bin/sh -c '/opt/vts/render-configmap.sh /opt/vts/config/vts.env > /opt/vts/vts-configmap.yaml && chmod 600 /opt/vts/vts-configmap.yaml'
ExecStartPre=-/usr/bin/podman kube down /opt/vts/vts.yaml
ExecStart=/usr/bin/podman kube play \
    --replace \
    --service-container=true \
    --network=podman \
    --log-driver=journald \
    --configmap /opt/vts/vts-configmap.yaml \
    /opt/vts/vts.yaml
ExecStop=/usr/bin/podman kube down /opt/vts/vts.yaml
Type=notify
NotifyAccess=all

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Confirm the flags this unit relies on exist**

Run: `podman kube play --help | grep -E "configmap|service-container|replace"`
Expected: `--configmap`, `--service-container`, `--replace` all present. (`--env-file` does NOT exist on
podman 5.7.0 — that is why the ConfigMap route is used; see Task 2a.)

- [ ] **Step 3: Verify the unit file is syntactically valid**

Run: `systemd-analyze verify systemd/vts.service 2>&1 | head; echo "exit=$?"`
Expected: no fatal parse errors (warnings about the unit not being installed are fine).

- [ ] **Step 4: Commit**

```bash
git add systemd/vts.service
git commit -m "feat(deploy): vts.service playing the kube pod (vts-0pg)"
```

---

## Task 4: Thin oneshot units for per-container restart

`systemctl restart vts` cycles the whole pod, which would needlessly kill an in-flight transcription when only the API needs bouncing. These keep `systemctl` as the single operator interface for the finer-grained action.

**Files:**
- Create: `systemd/vts-worker-restart.service`
- Create: `systemd/vts-webapi-restart.service`

**Interfaces:**
- Consumes: container names `vts-worker` / `vts-webapi` produced by Task 2.
- Produces: `systemctl start vts-worker-restart` and `systemctl start vts-webapi-restart`.

- [ ] **Step 1: Create `systemd/vts-worker-restart.service`**

```ini
[Unit]
Description=Restart only the VTS worker container inside the vts pod
Documentation=man:podman-restart(1)
# Pointless unless the pod is up; this does not start it.
After=vts.service
BindsTo=vts.service

[Service]
Type=oneshot
RemainAfterExit=no
# Restarting one container leaves its neighbour untouched and the pod Running
# (verified on podman 5.7.0). The worker requeues in-flight tasks on start, so
# only do this when that cost is acceptable.
ExecStart=/usr/bin/podman restart vts-worker
```

- [ ] **Step 2: Create `systemd/vts-webapi-restart.service`**

```ini
[Unit]
Description=Restart only the VTS web API container inside the vts pod
Documentation=man:podman-restart(1)
After=vts.service
BindsTo=vts.service

[Service]
Type=oneshot
RemainAfterExit=no
# Does not disturb the worker: an in-flight transcription keeps running.
ExecStart=/usr/bin/podman restart vts-webapi
```

- [ ] **Step 3: Verify both parse**

Run: `for u in systemd/vts-worker-restart.service systemd/vts-webapi-restart.service; do systemd-analyze verify "$u" 2>&1 | head -3; done`
Expected: no fatal parse errors.

- [ ] **Step 4: Commit**

```bash
git add systemd/vts-worker-restart.service systemd/vts-webapi-restart.service
git commit -m "feat(deploy): oneshot units to restart a single container (vts-0pg)"
```

---

## Task 5: Rehearse the pod under a throwaway name

Prove the manifest actually works **before** touching the running service. This plays the same YAML as a differently-named pod with no host port, so it cannot collide with production.

**Files:** none (host-only rehearsal)

- [ ] **Step 1: Build a staging copy of the manifest**

```bash
mkdir -p /tmp/vts-stage
sed -e 's/^  name: vts$/  name: vtsstage/' \
    -e '/hostPort: 8086/d' deploy/vts.yaml > /tmp/vts-stage/vts-stage.yaml
grep -nE "name: vtsstage|hostPort" /tmp/vts-stage/vts-stage.yaml
```
Expected: the pod is named `vtsstage` and no `hostPort` line remains.

- [ ] **Step 2: Render the ConfigMap and play it**

```bash
sudo ./scripts/render-configmap.sh /opt/vts/config/vts.env > /tmp/vts-stage/cm.yaml
sudo chmod 600 /tmp/vts-stage/cm.yaml
sudo podman kube play --replace --network=podman \
  --configmap /tmp/vts-stage/cm.yaml /tmp/vts-stage/vts-stage.yaml
```
Expected: pod created, three containers referenced (one init + two).
The staging ConfigMap holds real secrets — delete it in Step 6.

- [ ] **Step 3: Confirm the initContainer ran migrations and exited 0**

```bash
sudo podman logs vtsstage-migrate 2>&1 | tail -20
sudo podman inspect vtsstage-migrate --format '{{.State.ExitCode}}'
```
Expected: alembic output ending in `migrations applied`, exit code `0`.
If it exits non-zero, stop: the migrate role or the env file is wrong, and production must not be switched.

- [ ] **Step 4: Confirm both main containers are up and healthy**

```bash
sudo podman ps --format "{{.Names}}\t{{.Status}}" | grep vtsstage
sudo podman logs vtsstage-webapi 2>&1 | tail -10
sudo podman logs vtsstage-worker 2>&1 | tail -10
```
Expected: both `Up`; webapi shows uvicorn listening on 8080; worker shows its startup lines without a traceback.

- [ ] **Step 5: Confirm the pod can reach the diarization sidecar**

The sidecar is currently `inactive`, so start it first or this yields a false negative.

```bash
sudo systemctl start vts-diarization.service
sudo podman exec vtsstage-worker python -c "import httpx,os; u=os.environ['VTS_DIARIZATION_URL']; print(httpx.get(u+'/health', timeout=10).status_code)"
```
Expected: `200`. If the sidecar has no `/health`, substitute the path it does serve; a connection error is the failure that matters here, not a 404.

- [ ] **Step 6: Tear the rehearsal down**

```bash
sudo podman kube down /tmp/vts-stage/vts-stage.yaml
sudo podman pod rm -f vtsstage 2>/dev/null
sudo rm -rf /tmp/vts-stage          # contains a ConfigMap with real secrets
sudo podman pod ps --format "{{.Name}}"
```
Expected: `vtsstage` gone, staging directory removed; production containers untouched (`vts-webapi`,
`vts-worker` still `Up` — verify with `sudo podman ps`).

---

## Task 6: Cut production over to the pod

**This is the only irreversible-feeling step, and it briefly stops the service.** Everything before it was additive.

**Files:** none in repo (host installation)

- [ ] **Step 1: Confirm nothing is in flight**

```bash
sudo podman exec vts-webapi python -c "
import asyncio
from sqlalchemy import select, func
from vts.db.session import SessionLocal
from vts.db.models import Task, TaskStatus
async def main():
    async with SessionLocal() as s:
        for st in (TaskStatus.running, TaskStatus.queued, TaskStatus.waiting, TaskStatus.awaiting_input):
            print(st.value, await s.scalar(select(func.count()).select_from(Task).where(Task.status==st)))
asyncio.run(main())"
```
Expected: all zero. If anything is `running`, either wait for it or accept that it will be requeued and re-run from scratch.

- [ ] **Step 2: Install the manifest and units on the host**

The existing units are symlinks from `/etc/systemd/system` into the repo; keep that convention.

```bash
sudo cp deploy/vts.yaml /opt/vts/vts.yaml
# The unit re-renders the ConfigMap on every start, so the generator must live
# at a stable host path (not inside a git checkout that may move).
sudo cp scripts/render-configmap.sh /opt/vts/render-configmap.sh
sudo chmod 755 /opt/vts/render-configmap.sh
REPO="$(pwd)"
sudo ln -sf "$REPO/systemd/vts.service"              /etc/systemd/system/vts.service
sudo ln -sf "$REPO/systemd/vts-worker-restart.service" /etc/systemd/system/vts-worker-restart.service
sudo ln -sf "$REPO/systemd/vts-webapi-restart.service" /etc/systemd/system/vts-webapi-restart.service
sudo systemctl daemon-reload
```

Verify the generator works from its installed path before starting anything:

```bash
sudo /opt/vts/render-configmap.sh /opt/vts/config/vts.env | head -4
```
Expected: the ConfigMap header (`apiVersion`, `kind: ConfigMap`, `name: vts-env`).

- [ ] **Step 3: Stop the old units**

```bash
sudo systemctl disable --now vts-webapi.service vts-worker.service
sudo podman ps --format "{{.Names}}" | grep -E "^vts-(webapi|worker)$" || echo "old containers gone"
```
Expected: `old containers gone`.

- [ ] **Step 4: Start the pod**

```bash
sudo systemctl enable --now vts.service
sudo systemctl status vts.service --no-pager | head -15
```
Expected: `active (running)`. If it hangs on start, `Type=notify` is mismatched — see the rollback in Step 7.

- [ ] **Step 5: Verify the service actually serves**

```bash
sudo podman ps --format "{{.Names}}\t{{.Status}}" | grep -E "vts-(webapi|worker)"
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8086/
sudo podman logs vts-migrate 2>&1 | tail -5
```
Expected: both containers `Up`; HTTP status is whatever the app returns for `/` today (a redirect or 200 — a connection refusal is the failure); migrate log ends in `migrations applied`.

- [ ] **Step 6: Verify per-container restart works on the real pod**

```bash
sudo systemctl start vts-webapi-restart.service
sudo podman ps --format "{{.Names}}\t{{.Status}}" | grep -E "vts-(webapi|worker)"
```
Expected: `vts-webapi` uptime resets, `vts-worker` uptime does NOT.

- [ ] **Step 7: Know the rollback before you need it**

If any check above fails:

```bash
sudo systemctl disable --now vts.service
sudo podman pod rm -f vts 2>/dev/null
sudo systemctl enable --now vts-webapi.service vts-worker.service
sudo systemctl status vts-webapi.service vts-worker.service --no-pager | head
```
The old units are still on disk until Task 8, so this restores the previous topology.

---

## Task 7: Point the deploy workflow at the single unit

**Files:**
- Modify: `.github/workflows/deploy-after-build.yml`
- Create: `docs/deploy-pod.md`

**Interfaces:**
- Consumes: `vts.service` from Task 3.
- Produces: a deploy step that restarts one unit. **`WEBAPI_SERVICE` / `WORKER_SERVICE` GitHub Variables become unused** — the repo owner must remove or repoint them by hand; CI cannot do that.

- [ ] **Step 1: Find the restart block**

Run: `grep -n "WEBAPI_SERVICE\|WORKER_SERVICE\|systemctl restart\|systemctl status" .github/workflows/deploy-after-build.yml`
Note every line number — the variables appear both in the `env:` block and inside the remote heredoc.

- [ ] **Step 2: Replace the two restarts with one**

In the remote script, replace the pair of restarts and the pair of status checks:

```bash
sudo systemctl restart "${VTS_SERVICE}"
sudo systemctl status "${VTS_SERVICE}" --no-pager
```

and in the shell that builds the remote invocation, replace the two service variables with one:

```bash
vts_service="${VTS_SERVICE:-vts.service}"
```

passing `VTS_SERVICE='${vts_service}'` through to the remote shell instead of `WEBAPI_SERVICE`/`WORKER_SERVICE`. In the workflow `env:` block, replace the two `${{ vars.WEBAPI_SERVICE }}` / `${{ vars.WORKER_SERVICE }}` entries with `VTS_SERVICE: ${{ vars.VTS_SERVICE }}`.

- [ ] **Step 3: Check the workflow YAML still parses**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-after-build.yml')); print('workflow YAML OK')"`
Expected: `workflow YAML OK`.

- [ ] **Step 4: Confirm no stale references remain**

Run: `grep -n "WEBAPI_SERVICE\|WORKER_SERVICE" .github/workflows/deploy-after-build.yml; echo "exit=$?"`
Expected: no matches (`exit=1`).

- [ ] **Step 5: Write the operator doc**

Create `docs/deploy-pod.md`:

```markdown
# VTS pod topology — operator commands

VTS runs as a single podman pod (`vts`) played by `vts.service` from
`/opt/vts/vts.yaml`. Containers inside it are `vts-webapi` and `vts-worker`;
migrations run once per start in the `vts-migrate` initContainer.

| Task | Command |
|---|---|
| Deploy / restart everything | `sudo systemctl restart vts` |
| Restart only the worker | `sudo systemctl start vts-worker-restart` |
| Restart only the web API | `sudo systemctl start vts-webapi-restart` |
| Stop the worker for maintenance | `sudo podman stop vts-worker` |
| Bring it back | `sudo podman start vts-worker` |
| Logs (one container) | `sudo podman logs -f vts-worker` |
| Migration output of the last start | `sudo podman logs vts-migrate` |
| Pod state | `sudo podman pod ps` / `sudo podman ps --pod` |

## Configuration

`/opt/vts/config/vts.env` stays the single source of truth. `podman kube play`
cannot read an env file (it has no `--env-file`, only `--configmap`), so
`vts.service` re-renders `/opt/vts/vts-configmap.yaml` from `vts.env` on every
start via `/opt/vts/render-configmap.sh`.

To change configuration: edit `vts.env`, then `sudo systemctl restart vts`.
Never edit `vts-configmap.yaml` by hand — it is overwritten on the next start.
It contains secrets and is mode `600`; it must never be copied into the repo.

Notes:

- Restarting the worker requeues whatever it was processing: in-flight tasks go
  back to `queued` and re-run from scratch. Prefer `vts-webapi-restart` when
  only the API is misbehaving.
- A container stopped by hand is NOT resurrected by `restartPolicy: Always`
  (that policy covers crashes, not deliberate stops). The pod shows `Degraded`
  until you start it again — the other container keeps serving.
- The diarization sidecar is a separate unit (`vts-diarization.service`) on
  purpose: its image has its own build pipeline, it must not see app secrets,
  and it is optional. It is unaffected by pod restarts.
- Rollback to the old two-unit topology: `sudo systemctl disable --now vts`,
  then re-enable `vts-webapi.service` and `vts-worker.service` (kept in git
  history if already deleted).
```

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/deploy-after-build.yml docs/deploy-pod.md
git commit -m "ci(deploy): restart the single vts unit; document pod operations (vts-0pg)"
```

---

## Task 8: Retire the old units

Only after Task 6 has been confirmed working and the service has been up long enough to trust.

**Files:**
- Delete: `systemd/vts-webapi.service`
- Delete: `systemd/vts-worker.service`

- [ ] **Step 1: Confirm the pod has been healthy and the old units are inactive**

```bash
sudo systemctl is-active vts.service
sudo systemctl is-enabled vts-webapi.service vts-worker.service 2>&1
sudo podman ps --format "{{.Names}}\t{{.Status}}" | grep -E "vts-(webapi|worker)"
```
Expected: `active`; the old units report `disabled` (or `Failed to get unit file state`, if the symlinks were already removed); both containers `Up`.

- [ ] **Step 2: Remove the host symlinks**

```bash
sudo rm -f /etc/systemd/system/vts-webapi.service /etc/systemd/system/vts-worker.service
sudo systemctl daemon-reload
```

- [ ] **Step 3: Delete the unit files from the repo**

```bash
git rm systemd/vts-webapi.service systemd/vts-worker.service
```

- [ ] **Step 4: Verify nothing still references them**

Run: `grep -rn "vts-webapi.service\|vts-worker.service" --include="*.yml" --include="*.sh" --include="*.md" . | grep -v "docs/deploy-pod.md" | grep -v "^./docs/superpowers/"; echo "exit=$?"`
Expected: only the deliberate rollback mention in `docs/deploy-pod.md` and the spec/plan docs; `deploy.sh` must be checked by hand — it defaults `WEBAPI_SERVICE`/`WORKER_SERVICE`, and if it is still used it needs the same one-unit treatment as the workflow.

- [ ] **Step 5: Commit**

```bash
git add -A systemd/
git commit -m "chore(deploy): retire the pre-pod webapi/worker units (vts-0pg)"
```

---

## Self-Review

**Spec coverage:**

| Spec item | Task |
|---|---|
| Pod with two containers (webapi + worker) | Task 2 |
| Migrations in their own initContainer | Task 1 (role), Task 2 (initContainer) |
| `vts.service` playing the manifest | Task 3 |
| Oneshot units for per-container restart | Task 4 |
| Deploy restarts the pod as a whole | Task 7 |
| Secrets stay in `vts.env`, not in git | Task 2a (host-side generator), Task 2 Step 4 (leak check on the committed manifest) |
| hostPort 8086→8080 preserved | Task 2, verified Task 6 Step 5 |
| Volumes `/opt/vts`, `/disk/vts-data` | Task 2 |
| `podman pull` on restart (deploy picks up `:latest`) | Task 3 (`ExecStartPre`) |
| Diarization untouched, reachable from the pod | Task 5 Step 5 (connectivity check) |
| Rollback path | Task 6 Step 7; old units survive until Task 8 |
| Operator command documentation | Task 7 Step 5 |
| Risk: `Type=notify` mismatch | Task 6 Step 4 (hang ⇒ rollback) |
| Risk: GitHub Variables need manual edit | Task 7 Interfaces (called out as an owner action) |

**Placeholder scan:** none. One step deliberately branches on a human decision rather than prescribing an
outcome — Task 8 Step 4 (`deploy.sh` still defaults to the old service variables, and whether it is still
used is not knowable from the repo). It names the exact check and the exact consequence.

**Correction folded in while writing:** an earlier draft passed `--env-file` to `podman kube play`. That
flag **does not exist** (verified against podman 5.7.0 on this host — only `--configmap` does), so the
plan would have failed at Task 6. Hence Task 2a: the ConfigMap is generated on the host from `vts.env`,
git holds only the generator, and `vts.service` re-renders it on every start so it cannot drift from its
source.

**Type consistency:** container names are `vts-webapi` / `vts-worker` throughout (pod `vts` + container `webapi`/`worker`), matching the names Task 4's units restart and Task 6 verifies. The initContainer is `vts-migrate` wherever logs are read. `VTS_ROLE` values are `migrate` / `webapi` / `worker` / `both` in both the entrypoint and the manifest.

**Known gap, deliberately left:** `deploy.sh` (the manual deploy path) still defaults to `WEBAPI_SERVICE`/`WORKER_SERVICE`. Task 8 Step 4 surfaces it rather than silently rewriting a script whose current use is unknown.

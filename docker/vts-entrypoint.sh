#!/usr/bin/env sh
set -eu

UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN="${VTS_UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN:-15}"

# Verify superuser-provisioned preconditions (pgvector) before migrating.
# Migrations run as the unprivileged app role, so a missing extension would
# otherwise surface as an asyncpg traceback plus a systemd crash loop.
migrate() {
  python -m vts.db.preflight
  # Migrating from inside the image IS the deliberate case, so state the intent
  # the guard asks for. It only matters when config.yaml marks this deployment
  # productive; the guard exists to stop a stray `alembic upgrade head` in a
  # checkout on the prod host, not this one (vts-66i).
  VTS_ALLOW_PROD_MIGRATIONS=1 alembic upgrade head
}

start_webapi() {
  # webapi migrates by DEFAULT, because in the two-unit topology nothing else
  # does — worker never migrated. The pod sets VTS_SKIP_MIGRATIONS=1 on this
  # container, since its `migrate` initContainer has already run and both
  # containers start in parallel there.
  #
  # Defaulting the other way round would silently leave the old topology with no
  # migrations at all for as long as both topologies coexist.
  if [ "${VTS_SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "skipping migrations (VTS_SKIP_MIGRATIONS=1; the pod's init container owns them)"
  else
    migrate
  fi
  exec uvicorn vts.api.main:app --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips "*" \
    --timeout-graceful-shutdown "${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN}"
}

start_worker() {
  exec python -m vts.worker.main
}

start_both() {
  migrate
  python -m vts.worker.main &
  worker_pid="$!"
  trap 'kill "${worker_pid}" 2>/dev/null || true' INT TERM EXIT
  uvicorn vts.api.main:app --host 0.0.0.0 --port 8080 \
    --proxy-headers --forwarded-allow-ips "*" \
    --timeout-graceful-shutdown "${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN}"
  status="$?"
  kill "${worker_pid}" 2>/dev/null || true
  wait "${worker_pid}" 2>/dev/null || true
  exit "${status}"
}

# Run-once role for the pod's initContainer: apply migrations, then exit 0 so
# podman proceeds to the main containers. webapi no longer migrates, because in
# a pod it starts in parallel with worker and neither may run against an
# unmigrated schema.
run_migrate() {
  migrate
  echo "migrations applied"
}

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

case "${VTS_ROLE:-webapi}" in
  webapi)
    start_webapi
    ;;
  worker)
    start_worker
    ;;
  both)
    start_both
    ;;
  migrate)
    run_migrate
    ;;
  *)
    echo "Unsupported VTS_ROLE='${VTS_ROLE:-}'. Use webapi, worker, both, or migrate." >&2
    exit 1
    ;;
esac

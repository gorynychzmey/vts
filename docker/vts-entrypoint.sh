#!/usr/bin/env sh
set -eu

UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN="${VTS_UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN:-15}"

# Verify superuser-provisioned preconditions (pgvector) before migrating.
# Migrations run as the unprivileged app role, so a missing extension would
# otherwise surface as an asyncpg traceback plus a systemd crash loop.
migrate() {
  python -m vts.db.preflight
  alembic upgrade head
}

start_webapi() {
  migrate
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
  *)
    echo "Unsupported VTS_ROLE='${VTS_ROLE:-}'. Use webapi, worker, or both." >&2
    exit 1
    ;;
esac

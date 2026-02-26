#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/opt/vts}"
WEBAPI_SERVICE="${WEBAPI_SERVICE:-vts-webapi.service}"
WORKER_SERVICE="${WORKER_SERVICE:-vts-worker.service}"

if [[ -z "${SSH_HOST}" ]]; then
  echo "Set SSH_HOST to deployment target"
  exit 1
fi

echo "Bumping MINOR version before deployment"
python scripts/bump_version.py minor
NEW_VERSION="$(python scripts/get_version.py)"

echo "Running tests"
python -m pytest -q

echo "Committing version bump ${NEW_VERSION}"
git add vts/__init__.py
git commit -m "chore: deploy ${NEW_VERSION}"
git push

echo "Building and pushing images"
./build.sh

echo "Deploying on ${SSH_USER}@${SSH_HOST}"
ssh "${SSH_USER}@${SSH_HOST}" bash -s <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"
podman compose pull
sudo systemctl restart "${WEBAPI_SERVICE}"
sudo systemctl restart "${WORKER_SERVICE}"
sudo systemctl status "${WEBAPI_SERVICE}" --no-pager
sudo systemctl status "${WORKER_SERVICE}" --no-pager
EOF

echo "Deployment complete"



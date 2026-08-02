#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/opt/vts}"
VTS_SERVICE="${VTS_SERVICE:-vts.service}"

if [[ -z "${SSH_HOST}" ]]; then
  echo "Set SSH_HOST to deployment target"
  exit 1
fi

echo "Bumping MINOR version before deployment"
python scripts/bump_version.py minor
NEW_VERSION="$(python scripts/get_version.py)"

echo "Committing version bump ${NEW_VERSION}"
git add vts/__init__.py
git commit -m "chore: deploy ${NEW_VERSION}"
git push

echo "Building, testing, and pushing images"
./build.sh

echo "Deploying on ${SSH_USER}@${SSH_HOST}"
ssh "${SSH_USER}@${SSH_HOST}" bash -s <<EOF
set -euo pipefail
cd "${REMOTE_DIR}"
set -a
source /opt/vts/config/vts.env
set +a
if [[ -n "\${VTS_IMAGE:-}" ]]; then
  image="\${VTS_IMAGE}"
elif [[ -n "\${WEBAPI_IMAGE:-}" ]]; then
  image="\${WEBAPI_IMAGE}"
elif [[ -n "\${WORKER_IMAGE:-}" ]]; then
  image="\${WORKER_IMAGE}"
else
  echo "Set VTS_IMAGE in /opt/vts/config/vts.env"
  exit 1
fi
podman pull "\${image}"
sudo systemctl restart "${VTS_SERVICE}"
sudo systemctl status "${VTS_SERVICE}" --no-pager
EOF

echo "Deployment complete"

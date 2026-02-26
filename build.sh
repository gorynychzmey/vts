#!/usr/bin/env bash
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
IMAGE_REPO="${IMAGE_REPO:-docker.io/gorynychzmey/vts}"
APT_MIRROR="${APT_MIRROR:-http://deb.debian.org/debian}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-http://deb.debian.org/debian-security}"
VERSION="$(python scripts/get_version.py)"

WEBAPI_IMAGE="${IMAGE_REPO}:${VERSION}-webapi"
WORKER_IMAGE="${IMAGE_REPO}:${VERSION}-worker"
WEBAPI_LATEST="${IMAGE_REPO}:latest-webapi"
WORKER_LATEST="${IMAGE_REPO}:latest-worker"

echo "Building version ${VERSION}"
echo "APT_MIRROR=${APT_MIRROR}"
echo "APT_SECURITY_MIRROR=${APT_SECURITY_MIRROR}"

"${ENGINE}" build \
  -f docker/webapi.Dockerfile \
  --build-arg VTS_VERSION="${VERSION}" \
  --build-arg APT_MIRROR="${APT_MIRROR}" \
  --build-arg APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR}" \
  -t "${WEBAPI_IMAGE}" \
  -t "${WEBAPI_LATEST}" .

"${ENGINE}" build \
  -f docker/worker.Dockerfile \
  --build-arg VTS_VERSION="${VERSION}" \
  --build-arg APT_MIRROR="${APT_MIRROR}" \
  --build-arg APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR}" \
  -t "${WORKER_IMAGE}" \
  -t "${WORKER_LATEST}" .

echo "Pushing images"
"${ENGINE}" push "${WEBAPI_IMAGE}"
"${ENGINE}" push "${WEBAPI_LATEST}"
"${ENGINE}" push "${WORKER_IMAGE}"
"${ENGINE}" push "${WORKER_LATEST}"

echo "Done"

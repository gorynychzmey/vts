#!/usr/bin/env bash
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
REGISTRY="${REGISTRY:-docker.io}"
NAMESPACE="${NAMESPACE:-yourdockerhub}"
VERSION="$(python scripts/get_version.py)"

WEBAPI_IMAGE="${REGISTRY}/${NAMESPACE}/vts-webapi:${VERSION}"
WORKER_IMAGE="${REGISTRY}/${NAMESPACE}/vts-worker:${VERSION}"
WEBAPI_LATEST="${REGISTRY}/${NAMESPACE}/vts-webapi:latest"
WORKER_LATEST="${REGISTRY}/${NAMESPACE}/vts-worker:latest"

echo "Building version ${VERSION}"

"${ENGINE}" build \
  -f docker/webapi.Dockerfile \
  --build-arg VTS_VERSION="${VERSION}" \
  -t "${WEBAPI_IMAGE}" \
  -t "${WEBAPI_LATEST}" .

"${ENGINE}" build \
  -f docker/worker.Dockerfile \
  --build-arg VTS_VERSION="${VERSION}" \
  -t "${WORKER_IMAGE}" \
  -t "${WORKER_LATEST}" .

echo "Pushing images"
"${ENGINE}" push "${WEBAPI_IMAGE}"
"${ENGINE}" push "${WEBAPI_LATEST}"
"${ENGINE}" push "${WORKER_IMAGE}"
"${ENGINE}" push "${WORKER_LATEST}"

echo "Done"


#!/usr/bin/env bash
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
IMAGE_REPO="${IMAGE_REPO:-docker.io/gorynychzmey/vts}"
APT_MIRROR="${APT_MIRROR:-http://deb.debian.org/debian}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-http://deb.debian.org/debian-security}"
USE_BUILDX="${USE_BUILDX:-auto}"
BUILDX_CACHE_REPO="${BUILDX_CACHE_REPO:-${IMAGE_REPO}}"
BUILDX_CACHE_MODE="${BUILDX_CACHE_MODE:-max}"
BUILDX_PLATFORM="${BUILDX_PLATFORM:-}"
BUILDX_PROGRESS="${BUILDX_PROGRESS:-auto}"
VERSION="$(python scripts/get_version.py)"

WEBAPI_IMAGE="${IMAGE_REPO}:${VERSION}-webapi"
WORKER_IMAGE="${IMAGE_REPO}:${VERSION}-worker"
WEBAPI_LATEST="${IMAGE_REPO}:latest-webapi"
WORKER_LATEST="${IMAGE_REPO}:latest-worker"

echo "Building version ${VERSION}"
echo "APT_MIRROR=${APT_MIRROR}"
echo "APT_SECURITY_MIRROR=${APT_SECURITY_MIRROR}"

if [[ -f /proc/version ]] && grep -qi "microsoft" /proc/version; then
  case "$(pwd)" in
    /mnt/*)
      echo "WARNING: WSL build from /mnt/* is slower. Prefer repo under /home/<user>/..."
      ;;
  esac
fi

use_buildx=false
if [[ "${ENGINE}" == "docker" ]]; then
  if docker buildx version >/dev/null 2>&1; then
    case "${USE_BUILDX}" in
      auto|true)
        use_buildx=true
        ;;
      false)
        use_buildx=false
        ;;
      *)
        echo "Invalid USE_BUILDX value: ${USE_BUILDX} (expected: auto|true|false)"
        exit 1
        ;;
    esac
  elif [[ "${USE_BUILDX}" == "true" ]]; then
    echo "USE_BUILDX=true but docker buildx is not available"
    exit 1
  fi
fi

if [[ "${use_buildx}" == "true" ]]; then
  echo "Build mode: docker buildx + registry cache"
  echo "BUILDX_CACHE_REPO=${BUILDX_CACHE_REPO}"
  echo "BUILDX_CACHE_MODE=${BUILDX_CACHE_MODE}"
  common_args=(
    --build-arg "VTS_VERSION=${VERSION}"
    --build-arg "APT_MIRROR=${APT_MIRROR}"
    --build-arg "APT_SECURITY_MIRROR=${APT_SECURITY_MIRROR}"
    --progress "${BUILDX_PROGRESS}"
  )
  if [[ -n "${BUILDX_PLATFORM}" ]]; then
    common_args+=(--platform "${BUILDX_PLATFORM}")
  fi

  docker buildx build \
    -f docker/webapi.Dockerfile \
    "${common_args[@]}" \
    --cache-from "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-webapi" \
    --cache-to "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-webapi,mode=${BUILDX_CACHE_MODE}" \
    -t "${WEBAPI_IMAGE}" \
    -t "${WEBAPI_LATEST}" \
    --push .

  docker buildx build \
    -f docker/worker.Dockerfile \
    "${common_args[@]}" \
    --cache-from "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-worker" \
    --cache-to "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-worker,mode=${BUILDX_CACHE_MODE}" \
    -t "${WORKER_IMAGE}" \
    -t "${WORKER_LATEST}" \
    --push .
else
  echo "Build mode: classic ${ENGINE} build + push"
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
fi

echo "Done"

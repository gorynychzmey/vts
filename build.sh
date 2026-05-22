#!/usr/bin/env bash
set -euo pipefail

ENGINE="${CONTAINER_ENGINE:-podman}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/OWNER/vts}"
APT_MIRROR="${APT_MIRROR:-http://deb.debian.org/debian}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-http://deb.debian.org/debian-security}"
USE_BUILDX="${USE_BUILDX:-auto}"
BUILDX_CACHE_REPO="${BUILDX_CACHE_REPO:-${IMAGE_REPO}}"
BUILDX_CACHE_MODE="${BUILDX_CACHE_MODE:-max}"
BUILDX_PLATFORM="${BUILDX_PLATFORM:-}"
BUILDX_PROGRESS="${BUILDX_PROGRESS:-auto}"
VERSION_OVERRIDE="${VERSION_OVERRIDE:-}"
if [[ -n "${VERSION_OVERRIDE}" ]]; then
  VERSION="${VERSION_OVERRIDE}"
else
  VERSION="$(python scripts/get_version.py)"
fi

if ! [[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid version '${VERSION}'. Expected semver: X.Y.Z"
  exit 1
fi

VTS_IMAGE="${IMAGE_REPO}:${VERSION}"
VTS_LATEST="${IMAGE_REPO}:latest"
PYTEST_VERSION="${PYTEST_VERSION:-8.4.2}"
PYTEST_ASYNCIO_VERSION="${PYTEST_ASYNCIO_VERSION:-0.26.0}"

run_tests_in_container() {
  local runtime="${1}"
  local tests_dir="${PWD}/tests"
  local -a run_args
  run_args=(run --rm --entrypoint sh)
  if [[ -d "${tests_dir}" ]]; then
    run_args+=(-v "${tests_dir}:/app/tests:ro")
  else
    echo "Tests directory not found at ${tests_dir}"
    exit 1
  fi
  echo "Running tests inside container ${VTS_IMAGE}"
  "${runtime}" "${run_args[@]}" "${VTS_IMAGE}" -lc \
    "pip install -q pytest==${PYTEST_VERSION} pytest-asyncio==${PYTEST_ASYNCIO_VERSION} && python -m pytest -q tests"
}

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
  if [[ "${BUILDX_PLATFORM}" == *,* ]]; then
    echo "BUILDX_PLATFORM=${BUILDX_PLATFORM} is multi-platform."
    echo "Tests before push require loading image locally; use a single platform."
    exit 1
  fi
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
    -f docker/vts.Dockerfile \
    "${common_args[@]}" \
    --cache-from "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-vts" \
    --cache-to "type=registry,ref=${BUILDX_CACHE_REPO}:buildcache-vts,mode=${BUILDX_CACHE_MODE}" \
    -t "${VTS_IMAGE}" \
    -t "${VTS_LATEST}" \
    --load .

  run_tests_in_container "docker"

  echo "Pushing images"
  docker push "${VTS_IMAGE}"
  docker push "${VTS_LATEST}"
else
  echo "Build mode: classic ${ENGINE} build + push"
  "${ENGINE}" build \
    -f docker/vts.Dockerfile \
    --build-arg VTS_VERSION="${VERSION}" \
    --build-arg APT_MIRROR="${APT_MIRROR}" \
    --build-arg APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR}" \
    -t "${VTS_IMAGE}" \
    -t "${VTS_LATEST}" .

  run_tests_in_container "${ENGINE}"

  echo "Pushing images"
  "${ENGINE}" push "${VTS_IMAGE}"
  "${ENGINE}" push "${VTS_LATEST}"
fi

echo "Done"

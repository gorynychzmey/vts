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

  cleanup_smoke() { "${ENGINE}" rm -f "${name}" >/dev/null 2>&1 || true; }
  trap cleanup_smoke RETURN

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
    "${ENGINE}" logs "${name}" 2>&1 | tail -20 || true
    return 1
  fi

  "${ENGINE}" exec "${name}" python -c '
import io, json, math, struct, urllib.request, uuid, wave

sr = 16000
def tone(f0, dur):
    return [math.sin(2 * math.pi * f0 * (i / sr)) * 0.3 for i in range(int(sr * dur))]
samples = tone(110, 1.5) + [0.0] * int(sr * 0.3) + tone(220, 1.5)
buf = io.BytesIO()
w = wave.open(buf, "w")
w.setnchannels(1)
w.setsampwidth(2)
w.setframerate(sr)
w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))
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
assert {"segments", "embeddings", "num_speakers"} <= set(r.keys()), r.keys()
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
      *) echo "Invalid USE_BUILDX value: ${USE_BUILDX} (expected: auto|true|false)"; exit 1 ;;
    esac
  elif [[ "${USE_BUILDX}" == "true" ]]; then
    echo "USE_BUILDX=true but docker buildx is not available"
    exit 1
  fi
fi

if [[ "${use_buildx}" == "true" ]]; then
  echo "Build mode: docker buildx + registry cache"
  echo "BUILDX_CACHE_REPO=${BUILDX_CACHE_REPO}"
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

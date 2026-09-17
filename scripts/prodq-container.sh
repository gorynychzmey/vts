#!/usr/bin/env bash
# Run scripts/prodq.py inside the application image, in one line (vts-axm0).
#
# The same `podman run …` invocation was retyped at least 21 times in a day, in
# four slightly different spellings — and one of those spellings was wrong, so
# the query had to be run twice. The command is the same every time; only the
# SQL differs, which is what this wrapper makes true on the command line too.
#
#   scripts/prodq-container.sh 'SELECT count(*) FROM tasks'
#   scripts/prodq-container.sh --json 'SELECT id FROM tasks LIMIT 3'
#   scripts/prodq-container.sh --print 'SELECT 1'      # show, do not run
#
# Everything site-specific comes from the environment. That is not indirection
# for its own sake: this repository is PUBLIC, and an image name, a host path or
# a service name written down here would be an infrastructure map for anyone
# reading it. So there are no defaults to leak, and no fallbacks to guess with —
# an unset VTS_PRODQ_IMAGE is an error, not a shrug.
#
#   VTS_PRODQ_IMAGE        required. The image to run in.
#   VTS_PRODQ_MOUNTS       optional. Space-separated `-v` specs, e.g. the
#                          application config the script reads its DSN from.
#   VTS_PRODQ_ENGINE       optional, default `podman`.
#   VTS_PRODQ_ENGINE_ARGS  optional. Extra flags for the engine (`--network=host`).
#   VTS_PRODQ_SUDO         optional. Set to 1 for a rootful engine.
#   VTS_PRODQ_CONFIG       optional, default ~/.config/vts/prodq.env — sourced
#                          when it exists, so the values above live on the host
#                          and never in git.
#
# The script itself is mounted from THIS checkout, read-only: the code is the one
# being edited, the dependencies are the image's. Its path is derived from where
# this file sits, so no absolute path is written down either.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: prodq-container.sh [--print] [prodq flags] 'SQL'

  --print   assemble and show the command, run nothing
  Other flags (--write, --json) are passed to scripts/prodq.py unchanged.

Configuration comes from the environment; see the comments at the top of this
file, or set them in ~/.config/vts/prodq.env.
EOF
  exit 2
}

PRINT_ONLY=0
if [[ "${1:-}" == "--print" ]]; then
  PRINT_ONLY=1
  shift
fi

[[ $# -eq 0 ]] && usage

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${VTS_PRODQ_CONFIG:-$HOME/.config/vts/prodq.env}"
# shellcheck disable=SC1090 - operator-supplied, by design
[[ -f "$CONFIG" ]] && source "$CONFIG"

if [[ -z "${VTS_PRODQ_IMAGE:-}" ]]; then
  echo "prodq-container.sh: VTS_PRODQ_IMAGE is not set (and there is no default" >&2
  echo "  to fall back on — see the comments in this file, or $CONFIG)." >&2
  exit 3
fi

ENGINE="${VTS_PRODQ_ENGINE:-podman}"

cmd=()
[[ "${VTS_PRODQ_SUDO:-}" == "1" ]] && cmd+=("sudo" "-n")
cmd+=("$ENGINE" "run" "--rm")
# Word-split on purpose: these hold several arguments each.
# shellcheck disable=SC2206,SC2086
if [[ -n "${VTS_PRODQ_ENGINE_ARGS:-}" ]]; then
  read -r -a engine_args <<< "${VTS_PRODQ_ENGINE_ARGS}"
  cmd+=("${engine_args[@]}")
fi
cmd+=("-v" "${HERE}/prodq.py:/app/scripts/prodq.py:ro,Z")
if [[ -n "${VTS_PRODQ_MOUNTS:-}" ]]; then
  read -r -a mount_specs <<< "${VTS_PRODQ_MOUNTS}"
  for spec in "${mount_specs[@]}"; do
    cmd+=("-v" "$spec")
  done
fi
cmd+=("$VTS_PRODQ_IMAGE" "python" "scripts/prodq.py" "$@")

if [[ "$PRINT_ONLY" == "1" ]]; then
  # One line per argument: the boundaries are the point. A statement that has
  # been split is a different query, and on a single line that is invisible.
  printf '%s\n' "${cmd[@]}"
  exit 0
fi

exec "${cmd[@]}"

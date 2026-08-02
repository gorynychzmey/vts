#!/usr/bin/env bash
# Render a Kubernetes ConfigMap from a KEY=VALUE env file, for `podman kube play
# --configmap`. kube play has no --env-file, so this is how /opt/vts/config/vts.env
# reaches the pod (vts-0pg).
#
# The OUTPUT CONTAINS SECRETS (VTS_OAUTH_CLIENT_SECRET). Write it beside vts.env
# on the host with the same ownership/permissions — never into the repo.
#
#   sudo ./scripts/render-configmap.sh > /opt/vts/vts-configmap.yaml
set -euo pipefail

ENV_FILE="${1:-/opt/vts/config/vts.env}"
NAME="${CONFIGMAP_NAME:-vts-env}"

[ -r "$ENV_FILE" ] || { echo "cannot read $ENV_FILE" >&2; exit 1; }

printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: %s\ndata:\n' "$NAME"

# Only KEY=VALUE lines; skip blanks and comments. Values are emitted as
# single-quoted YAML scalars (internal ' doubled), which keeps #, :, spaces and
# URLs literal.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  case "$line" in
    *=*) ;;
    *) continue ;;
  esac
  key=${line%%=*}
  value=${line#*=}
  # NOTE the trailing '*' cases: `[A-Za-z_][A-Za-z_0-9]*` alone would require at
  # least TWO characters, silently dropping a single-letter key such as `A=1`.
  # Losing a variable without a word is the worst failure this script can have,
  # so an unusable key is a hard error, not a skip.
  case "$key" in
    [A-Za-z_]|[A-Za-z_][A-Za-z_0-9]*) ;;
    *) echo "refusing to render: unusable key '$key' in $ENV_FILE" >&2; exit 1 ;;
  esac
  # Strip one layer of surrounding quotes, as the shell would when sourcing.
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  escaped=$(printf '%s' "$value" | sed "s/'/''/g")
  printf "  %s: '%s'\n" "$key" "$escaped"
done < "$ENV_FILE"

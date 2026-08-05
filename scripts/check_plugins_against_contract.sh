#!/usr/bin/env bash
# Verify the plugin repo still works against the contract in THIS checkout.
#
# The delivery contract is a public interface: plugins live in
# gorynychzmey/vts-plugins, are built against it, and bind to it at load time.
# A change here can break a plugin that nobody rebuilt — and the failure would
# surface in production as a delivery that stops working, not as a red build.
#
# This runs LOCALLY, before the change is pushed, so a contract change that
# breaks the plugins is caught while it is still cheap to fix. It deliberately
# does NOT go through GitHub Actions: a workflow in vts cannot dispatch to
# another repository (the built-in GITHUB_TOKEN is scoped to the repo running
# it — verified: HTTP 403 "Resource not accessible by integration"), so the
# CI-side route would need a cross-repo PAT for no benefit over checking here.
#
# Usage:
#   scripts/check_plugins_against_contract.sh [path-to-vts-plugins]
#
# Exit codes:
#   0  plugins pass against this contract (or the check was skipped, see below)
#   1  a plugin FAILS against this contract — do not push without a look
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGINS_DIR="${1:-${VTS_PLUGINS_DIR:-$HOME/dev/vts-plugins}}"
CONTRACT_PKG="${REPO_ROOT}/packages/vts-delivery-contract"

if [[ ! -d "${PLUGINS_DIR}" ]]; then
  # Skipped, not failed: a VTS checkout is perfectly usable without the plugin
  # repo beside it, and this must not block someone who never touches plugins.
  echo "SKIP: no plugin checkout at ${PLUGINS_DIR}"
  echo "      clone https://github.com/gorynychzmey/vts-plugins to enable this check"
  exit 0
fi

contract_version="$(
  python3 - "${REPO_ROOT}" <<'PY'
import ast, pathlib, sys
src = pathlib.Path(sys.argv[1], "vts/delivery/contract.py").read_text()
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(
        getattr(t, "id", "") == "CONTRACT_VERSION" for t in node.targets
    ):
        print(".".join(str(v) for v in ast.literal_eval(node.value)))
        break
else:
    raise SystemExit("CONTRACT_VERSION not found in the contract module")
PY
)"
echo "contract in this checkout: ${contract_version}"

workdir="$(mktemp -d)"
trap 'rm -rf "${workdir}"' EXIT

python3 -m venv "${workdir}/venv" >/dev/null
PIP="${workdir}/venv/bin/pip"
PY_BIN="${workdir}/venv/bin/python"

# Install the contract from THIS working tree, not from git: the whole point is
# to test the change that has not been pushed yet.
echo "installing the local contract..."
"${PIP}" install --quiet "${CONTRACT_PKG}" || {
  echo "FAIL: the contract package does not build from this checkout"
  exit 1
}

failed=0
for plugin_toml in "${PLUGINS_DIR}"/*/pyproject.toml; do
  [[ -e "${plugin_toml}" ]] || continue
  plugin_dir="$(dirname "${plugin_toml}")"
  plugin_name="$(basename "${plugin_dir}")"
  echo ""
  echo "── ${plugin_name} ─────────────────────────────────────────"

  # --no-deps keeps pip from pulling the PUBLISHED contract over the local one
  # we just installed, which would make this check silently meaningless.
  "${PIP}" install --quiet --no-deps -e "${plugin_dir}" || {
    echo "FAIL: ${plugin_name} does not install"
    failed=1
    continue
  }
  "${PIP}" install --quiet pytest pytest-asyncio httpx >/dev/null

  if "${PY_BIN}" -m pytest "${plugin_dir}/tests" -q 2>&1 | tail -15; then
    echo "PASS: ${plugin_name} works against contract ${contract_version}"
  else
    echo "FAIL: ${plugin_name} fails against contract ${contract_version}"
    failed=1
  fi
done

echo ""
if [[ "${failed}" -ne 0 ]]; then
  echo "RESULT: a plugin is broken by the contract in this checkout."
  echo "        Fix the plugin in ${PLUGINS_DIR} (and release it) before"
  echo "        shipping this contract change."
  exit 1
fi
echo "RESULT: all plugins pass against contract ${contract_version}"

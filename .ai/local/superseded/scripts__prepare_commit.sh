#!/usr/bin/env bash
set -euo pipefail

# Remove transient pytest fallback cache directories from repo root.
find . -maxdepth 1 -type d -name "pytest-cache-files-*" -exec rm -rf {} +
rm -rf .pytest_cache

if [[ ! -x ".venv/bin/python3" ]]; then
  echo "ERROR: .venv not found. Run:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 1
fi

HASH_FILE=".venv/.requirements-hash"
CURRENT_HASH="$(sha256sum requirements-dev.txt | cut -d' ' -f1)"
STORED_HASH="$(cat "${HASH_FILE}" 2>/dev/null || true)"

if [[ "${CURRENT_HASH}" != "${STORED_HASH}" ]]; then
  echo "requirements-dev.txt changed, reinstalling..."
  .venv/bin/pip install -q -r requirements-dev.txt
  echo "${CURRENT_HASH}" > "${HASH_FILE}"
fi

python3 scripts/bump_version.py patch
.venv/bin/python3 -m pytest -q

git add -A

echo "Patch bumped, tests passed, changes staged. Create commit and push next."

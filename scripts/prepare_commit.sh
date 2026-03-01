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

python3 scripts/bump_version.py patch
.venv/bin/python3 -m pytest -q

git add -A

echo "Patch bumped, tests passed, changes staged. Create commit and push next."

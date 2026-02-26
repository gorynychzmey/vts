#!/usr/bin/env bash
set -euo pipefail

# Remove transient pytest fallback cache directories from repo root.
find . -maxdepth 1 -type d -name "pytest-cache-files-*" -exec rm -rf {} +
rm -rf .pytest_cache

python scripts/bump_version.py patch
python -m pytest -q
git add -A

echo "Patch bumped, tests passed, changes staged. Create commit and push next."

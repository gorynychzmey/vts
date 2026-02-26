#!/usr/bin/env bash
set -euo pipefail

python scripts/bump_version.py patch
python -m pytest -q
git add -A

echo "Patch bumped, tests passed, changes staged. Create commit and push next."


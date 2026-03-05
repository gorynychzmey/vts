# Git Workflow — VTS (see also [PROJECT_RULES.md](../PROJECT_RULES.md))

## Key paths
- Commit script: `bash scripts/prepare_commit.sh` (in project root, NOT `./prepare_commit.sh`)
- Tests: `.venv/bin/python3 -m pytest` (NOT `python` or `python3`)

## One-time local setup (if .venv missing)
```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

## Commit steps (every task)
1. `bash scripts/prepare_commit.sh` — bumps patch version + stages all changes
2. `git add <specific files>` — never blindly `git add -A`
3. `git commit -m "..."` with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
4. `git push`

## Build tag (only on explicit "build" request)
After commit+push: `git tag build-X.Y.Z && git push origin build-X.Y.Z` (X.Y.Z = current `vts/__init__.py`)

## Subagent prompt (model: haiku)

```
In /path/to/vts:
1. Run `bash scripts/prepare_commit.sh`
2. Run `git add <relevant files>`
3. Commit with message: "<message>"
4. Push to origin
Report the result.
```

Note: always specify `bash scripts/prepare_commit.sh` exactly — the script is in `scripts/`, not in the project root.

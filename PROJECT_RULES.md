# PROJECT_RULES

Contract document — keep in sync with automation scripts.

## 1. Git Workflow

- **Commit checklist:** tests pass → `python scripts/bump_version.py patch` → `bash scripts/prepare_commit.sh` → commit → push
- **`build` keyword** (after push): bump version commit, then `git tag build-X.Y.Z && git push origin build-X.Y.Z` (tag must match `vts/__init__.py`). Do NOT run `build.sh` locally.
- `scripts/prepare_commit.sh` removes `.pytest_cache`, `pytest-cache-files-*`

## 2. Semantic Versioning

Format `a.b.c` in `vts/__init__.py` (also `GET /api/version`, Docker label).

| Event | Action |
|---|---|
| Every commit | `python scripts/bump_version.py patch` |
| Deployment | `python scripts/bump_version.py minor` (resets patch) |
| Breaking change | bump MAJOR (manual) |

## 3. Deployment (manual, in order)

Bump MINOR → tests → commit → push → build images (`build.sh`) → push to Docker Hub → SSH → pull → restart via `systemd/*.service`. See [`docs/INITIAL_DEPLOYMENT.md`](docs/INITIAL_DEPLOYMENT.md).

## 4. Parallel Execution Constraints

- Max 2 transcription segments per task
- Heavy slot limit = 1 (env: `VTS_HEAVY_SLOT_LIMIT`)
- Light steps: unconstrained

## 5. Persistence of Rules

Keep in sync: this file, README workflow section, `scripts/bump_version.py`, `build.sh`, `deploy.sh`.

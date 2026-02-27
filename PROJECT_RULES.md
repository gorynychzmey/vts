# PROJECT_RULES

This document is part of the repository contract and must stay in sync with automation scripts.

## 1. Git Workflow

- Source of truth is GitHub.
- A task is complete only when:
  - checks/tests pass,
  - commit is created,
  - commit is pushed.
- If task wording includes `build` after commit/push, default action is GitHub Actions build trigger:
  - create and push tag `build-*` (for example `build-0.2.6`),
  - do not run local `build.sh` unless explicitly requested.
- Before commit, remove transient pytest cache directories (`pytest-cache-files-*`, `.pytest_cache`).
  - automated in `scripts/prepare_commit.sh`.

## 2. Semantic Versioning

Version format: `a.b.c`

Initial value: `0.0.0`

Rules:

- Before every commit: increment PATCH (`c`).
- Before every deployment: increment MINOR (`b`) and reset PATCH to `0`.
- MAJOR (`a`) is reserved for future breaking changes.

Version locations:

- `vts/__init__.py`
- `GET /api/version`
- Docker image label `org.opencontainers.image.version`

Automation:

- `python scripts/bump_version.py patch`
- `python scripts/bump_version.py minor`
- `scripts/prepare_commit.sh`

## 3. Deployment Procedure

Deployment is manual and must follow this exact order:

1. Bump MINOR version.
2. Run tests/checks.
3. Commit version bump.
4. Push to GitHub.
5. Build container images.
6. Push images to Docker Hub.
7. SSH to server.
8. Pull latest images.
9. Restart containers via systemd.

Scripts:

- `build.sh`
- `deploy.sh`
- `systemd/*.service`

Detailed first-time bootstrap instructions are maintained in `docs/INITIAL_DEPLOYMENT.md`.

## 4. Parallel Execution Constraints

- Max 2 transcription segments per task.
- Global heavy slot limit = 1 (configurable via `VTS_HEAVY_SLOT_LIMIT`).
- Light steps are unconstrained.

## 5. Persistence of Rules

These rules must remain persisted in:

- this file (`PROJECT_RULES.md`),
- README workflow section,
- automation scripts (`scripts/bump_version.py`, `build.sh`, `deploy.sh`).

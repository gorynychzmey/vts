# PROJECT_RULES

Contract document — authoritative source. Must stay consistent with automation scripts.

---

## 1. Git Workflow

- **Every task ends with commit + push** (mandatory).
- **One logical change per commit.** No mixed concerns.
- Working tree must be clean before starting a new task.
- No force-push to `main`.

### Commit Checklist (strict order)

1. Tests pass  
2. `python scripts/bump_version.py patch`  
3. `bash scripts/prepare_commit.sh`  
4. Commit  
5. Push  

`scripts/prepare_commit.sh` removes `.pytest_cache`, `pytest-cache-files-*`.

### Build Tag Flow

Keyword: `build` (after push)

1. Ensure version matches `vts/__init__.py`
2. `git tag build-X.Y.Z`
3. `git push origin build-X.Y.Z`
4. Start GitHub Actions monitoring in a background subagent immediately after tag push
5. Keep monitoring until the triggered workflow reaches final status and report result back in the task

Do **NOT** run `build.sh` locally unless explicitly required.

---

## 2. Semantic Versioning

Format: `a.b.c`  

Defined in:
- `vts/__init__.py`
- `GET /api/version`
- Docker image label

| Event | Action |
|-------|--------|
| Every commit | bump PATCH |
| Deployment | bump MINOR (resets PATCH) |
| Breaking change | bump MAJOR (manual, explicit justification required) |

### Rules

- Version must change before every commit.
- No commit without version bump.
- No deployment without MINOR bump commit.

---

## 3. Deployment (manual, strict order)

1. Bump MINOR  
2. Tests  
3. Commit  
4. Push  
5. `build.sh` (image build)  
6. Push to Docker Hub  
7. SSH to server  
8. Pull new image  
9. Restart via `systemd/*.service`

Reference: `docs/INITIAL_DEPLOYMENT.md`

Deployment must be reproducible and idempotent.

---

## 4. Parallel Execution Constraints

- Max 2 transcription segments per task  
- Heavy slot limit = 1 (`VTS_HEAVY_SLOT_LIMIT`)  
- Light steps: unlimited  
- No background processes bypassing slot limits  
- Resource limits must be enforced at runtime level (not only UI level)  

---

## 5. Codex Usage Rules (Mandatory)

To control weekly limits and ensure predictable diffs.

### Context Discipline

- Use only explicitly referenced files.
- Do not scan entire repository unless explicitly requested.
- Do not rewrite full modules.

### Output Format

- Default: unified diff only.
- Max 30 changed lines unless explicitly required.
- No explanations after diff.

### Scope Control

- Minimal fix first.
- No refactor unless explicitly requested.
- No API/signature changes without explicit instruction.
- No dependency additions without justification.

### Tests

- Do not create/modify tests unless requested.
- Do not run broad validation tasks unless required.

### Ambiguity

If requirements are unclear → request clarification before generating code.

---

## 6. Persistence of Rules

Must remain consistent across:

- `PROJECT_RULES`
- README workflow section
- `scripts/bump_version.py`
- `build.sh`
- `deploy.sh`
- CI configuration (if present)

Any workflow change requires updating all listed artifacts.

---

## 7. Integrity Constraints

- `main` branch must always be deployable.
- No partially implemented features committed.
- No temporary debug code.
- No commented-out legacy code without justification.
- No silent behavior changes.

---

This document is binding for all contributors and automation agents.

---
allowed-tools: Bash(bash scripts/prepare_commit.sh), Bash(git tag:*), Bash(git push origin build-*), Bash(cat vts/__init__.py), Bash(git log --oneline -5), Bash(git status)
description: Tag and push a build-X.Y.Z release tag for VTS
---

## Context

- Current version: !`python3 -c "import vts; print(vts.__version__)" 2>/dev/null || grep '__version__' vts/__init__.py`
- Recent commits: !`git log --oneline -5`
- Git status: !`git status --short`

## Your task

Create and push a build tag for the current VTS version.

**Steps:**
1. If the working tree is clean (no uncommitted changes), skip to step 3.
2. If there are uncommitted changes, tell the user and stop — do not commit automatically.
3. Read the current version from `vts/__init__.py` (e.g. `0.2.51`).
4. Run: `git tag build-<version>`
5. Run: `git push origin build-<version>`
6. Report the tag that was created and pushed.

Do not run `prepare_commit.sh`, do not bump the version, do not create a commit — only tag and push the tag.

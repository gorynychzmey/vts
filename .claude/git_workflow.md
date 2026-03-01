# Git Workflow — VTS (see also [PROJECT_RULES.md](../PROJECT_RULES.md))

## Commit steps (every task)
1. `python scripts/bump_version.py patch`
2. `bash scripts/prepare_commit.sh`
3. `git add <specific files>` — never blindly `git add -A`
4. `git commit -m "..."` with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
5. `git push`

## Build tag (only on explicit "build" request)
After commit+push: `git tag build-X.Y.Z && git push origin build-X.Y.Z` (X.Y.Z = current `vts/__init__.py`)

## Subagent prompt (model: haiku)

```
Working directory: /path/to/vts
Task: commit and push changes for this session.
1. python scripts/bump_version.py patch
2. bash scripts/prepare_commit.sh
3. git status && git log -3 --oneline
4. git add <relevant files>
5. git diff --cached
6. git commit -m "<message>\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
7. git push
8. Report commit hash and new version.
Context: <describe changes>
```

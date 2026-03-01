# Git Workflow — VTS (see also [PROJECT_RULES.md](../PROJECT_RULES.md))

## Commit steps (every task)
1. `bash scripts/prepare_commit.sh` — bumps patch version + stages all changes
2. `git add <specific files>` — never blindly `git add -A`
3. `git commit -m "..."` with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
4. `git push`

## Build tag (only on explicit "build" request)
After commit+push: `git tag build-X.Y.Z && git push origin build-X.Y.Z` (X.Y.Z = current `vts/__init__.py`)

## Subagent prompt (model: haiku)

```
Working directory: /path/to/vts
Task: commit and push changes for this session.
1. bash scripts/prepare_commit.sh
2. git status && git log -3 --oneline
3. git add <relevant files>
4. git diff --cached
5. git commit -m "<message>\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
6. git push
7. Report commit hash and new version.
Context: <describe changes>
```

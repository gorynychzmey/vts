---
allowed-tools: Bash(git tag:*), Bash(git push origin build-*), Bash(git status --short), Bash(grep * vts/__init__.py), Bash(gh run watch *), Bash(gh run list *), Bash(gh run view *)
description: Tag and push a build-X.Y.Z release tag for VTS, then wait for the workflow result
---

## Your task

Create and push a build tag for the current VTS version, then monitor the GitHub Actions workflows to completion.

## Execution note

If subagents are available, prefer running the long-lived GitHub Actions monitoring in a subagent so the main agent can continue other work while the workflows are pending. Use the main agent only for the local preflight checks, tag creation, and tag push; once the remote run IDs are known, hand off the `gh run list` / `gh run watch` polling and final success or failure reporting to the subagent. If the build result is immediately blocking the very next step, waiting in the main agent is allowed.

**Steps:**
1. Run `git status --short` — if there are uncommitted changes, tell the user and stop.
2. Read the current version from `vts/__init__.py` with `grep '__version__'`.
3. Run `git tag build-<version>`
4. Run `git push origin build-<version>`
5. Run `gh run list --workflow=build-images.yml --limit=1` and wait a moment for the run to appear (it may take a few seconds to register after the tag push — retry once if empty).
6. Run `gh run watch <run-id> --exit-status` to stream progress and wait for completion.
7. If build failed: show the failed step log with `gh run view <run-id> --log-failed`, summarise the error, and stop.
8. If build succeeded: wait for the deploy workflow to appear — run `gh run list --workflow=deploy-after-build.yml --limit=1` (retry a few times if not yet listed, the workflow_run trigger may take 10–20 seconds).
9. Run `gh run watch <deploy-run-id> --exit-status` to stream deploy progress and wait for completion.
10. Report the final result:
    - If both succeeded: "Build and deploy build-<version> completed successfully."
    - If deploy failed: show the failed step log with `gh run view <deploy-run-id> --log-failed` and summarise the error.

Do not run `prepare_commit.sh`, do not bump the version, do not create a commit — only tag, push, and monitor.

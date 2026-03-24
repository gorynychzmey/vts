<!-- internal-standards:managed -->
# Capability Index

Open individual capability files only when the current task needs them.

## Git Task Completion Policy

- Name: `git-task-completion-policy`
- Status: applicable
- Detail: `.ai/managed/capabilities/git-task-completion-policy.md`
- Summary: Reusable task-completion workflow covering checks, focused commits, and push expectations.

## Semantic Version Workflow

- Name: `semantic-version-workflow`
- Status: applicable
- Detail: `.ai/managed/capabilities/semantic-version-workflow.md`
- Summary: Provider-driven semantic versioning workflow with separate storage and bump policy.

## Build Tag Release Flow

- Name: `build-tag-release-flow`
- Status: applicable
- Detail: `.ai/managed/capabilities/build-tag-release-flow.md`
- Summary: Standardizes `build-X.Y.Z` tag-triggered release behavior.

## Docker Image Version Tagging

- Name: `docker-image-version-tagging`
- Status: applicable
- Detail: `.ai/managed/capabilities/docker-image-version-tagging.md`
- Summary: Requires versioned image tags alongside `latest` for managed image release flows.

## GitHub Actions Build Images

- Name: `github-actions-build-images`
- Status: applicable
- Detail: `.ai/managed/capabilities/github-actions-build-images.md`
- Summary: Reusable capability for GitHub Actions image build pipelines.

## SSH Post-Build Deploy

- Name: `ssh-post-build-deploy`
- Status: applicable
- Detail: `.ai/managed/capabilities/ssh-post-build-deploy.md`
- Summary: Reusable deploy-after-build flow using validated SSH secrets and remote service restart.

## Python Init Version Provider

- Name: `python-init-version-provider`
- Status: applicable
- Detail: `.ai/managed/capabilities/python-init-version-provider.md`
- Summary: Reads semantic version from a Python `__init__.py` assignment using a regex-based provider.

## Container Build With Tests

- Name: `container-build-with-tests`
- Status: applicable
- Detail: `.ai/managed/capabilities/container-build-with-tests.md`
- Summary: Adds build flows that run tests in or against the built image before push.

## Podman Systemd Deploy

- Name: `podman-systemd-deploy`
- Status: applicable
- Detail: `.ai/managed/capabilities/podman-systemd-deploy.md`
- Summary: Reusable deployment pattern for Podman-backed services managed by systemd.

## Postgres Bootstrap Script

- Name: `postgres-bootstrap-script`
- Status: applicable
- Detail: `.ai/managed/capabilities/postgres-bootstrap-script.md`
- Summary: Reusable database bootstrap capability for projects that need idempotent database setup scripts.

## Agent Build Command

- Name: `agent-build-command`
- Status: applicable
- Detail: `.ai/managed/capabilities/agent-build-command.md`
- Summary: Standardizes build command guidance for agents that trigger tag-based release flows.

## Context-Mode Agent Routing

- Name: `context-mode-agent-routing`
- Status: applicable
- Detail: `.ai/managed/capabilities/context-mode-agent-routing.md`
- Summary: Reusable shared routing rules for context-preserving agent tool usage.

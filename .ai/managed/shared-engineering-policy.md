<!-- internal-standards:managed -->
# Shared Engineering Policy

## Engineering First Source

- Managed standards generated from `internal-standards` are the first source for managed engineering rules.
- Prefer explicit files and reviewable diffs over hidden transforms.
- Never silently rewrite arbitrary files outside managed scope.
- Preserve project-specific extensions in `.ai/local/` instead of duplicating authoritative rules.
- If a local file duplicates a managed standard, replace it, migrate its local-only addendum, or mark it as superseded.

## Deployment and Release

- Build and deployment actions happen only on explicit user request.
- Build tags use `build-X.Y.Z` when the project enables build-tag releases.
- Deployment automation must validate secrets and required runtime variables before making remote changes.
- Release scripts and docs must stay consistent with the actual automation entrypoints.

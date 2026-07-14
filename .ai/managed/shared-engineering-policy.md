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

## Knowledge Capture

- Persist every reusable insight, finding, gotcha, or pattern to durable memory as the work happens — not only in ephemeral conversation. Whatever a project's issue/memory tracker records (e.g. a `remember`-style command), capture the same content.
- Additionally mirror that persistent knowledge into the shared Cognee dataset `development_knowledge`, which spans all projects. Use the Cognee `remember` tool with `dataset_name="development_knowledge"` and no `session_id` (permanent knowledge graph; a `session_id` writes to session cache only).
- Because `development_knowledge` is cross-project, begin each stored entry with an explicit project marker line (e.g. `Project: <name> (...)`). Retrieve with the Cognee `recall` tool scoped to `datasets="development_knowledge"`.
- The Cognee copy is additive, never a replacement for the project's own tracker. If the Cognee connector is unavailable in the current environment (e.g. headless/cron runs), record to the project tracker as usual and note the missed mirror.
- Do not create the dataset up front with a dedicated create call — the first `remember` for a new `dataset_name` creates it implicitly.

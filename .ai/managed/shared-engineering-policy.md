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
- Automate the mirror rather than relying on recall: this rule is routinely forgotten in practice, and a written rule cannot trigger itself. Where the harness supports hooks, wire the tracker's `remember` write to inject a reminder to mirror. In Claude Code: a `PostToolUse` hook with `matcher: "Bash"` and `if: "Bash(<tracker> remember*)"` whose script emits `hookSpecificOutput.additionalContext` naming the Cognee tool, the dataset, and the project marker.
- Keep such a hook a *reminder*, not a direct tool call, even where the harness can invoke tools itself (e.g. Claude Code's `type: "mcp_tool"`). The hook sees only the raw command string, so a direct call would store shell quoting instead of distilled knowledge and would drop the project marker. The agent must restate the insight.
- Hook hygiene: put the logic in a script file, never an inline one-liner — nested shell quoting silently mangles the filter, producing a hook that fires on every command. Match the tracker command only at the start of a command or after a shell separator (so `<tracker> list | grep remember` does not trigger), and always exit 0 so a failing reminder can never block the memory write. Verify by piping a synthetic payload through the script for both a matching and a non-matching command before shipping.

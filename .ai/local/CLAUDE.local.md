# Workflow rules

- **Commit + push after every task**: when a task is done, bump the version in `vts/__init__.py`, commit all changed files, and push to `origin/main`.
- **Build only on explicit request**: never run `/build` or push a `build-X.Y.Z` tag unless the user explicitly asks.
- **This repo is PUBLIC — check every commit for sensitive data before staging it.** Not a "be careful" reminder: run the check. Before `git commit`, review `git diff --staged` (and `git status` for newly tracked files) for
  - **Secrets**: keys, passwords, tokens, connection strings with credentials, `.env` contents.
  - **Internal infrastructure**: hostnames of real hosts (`*.fritz.box` and other LAN names), internal IPs, network topology, which services share a network, host paths that reveal layout, file modes.
  - **Third-party context**: names of unrelated services/containers running on the same host — they belong to other projects and are not ours to publish.

  Rules of thumb: docs and *tests* leak as easily as code — use `example.invalid` / RFC5737 addresses in fixtures instead of real hosts, and assert on the *shape* of a message, not on a real hostname. Tracker dumps (`.beads-backups/`), backups and pasted logs are written assuming privacy — never track them. A generic "prod" almost always carries the same meaning as the real hostname, so prefer it.

  Known recurring trap: `internal-standards sync` writes the real local checkout path into `.ai/standards-version.json` (`source_path`) and `.ai/bin/internal-standards`. Both are tracked, and both are kept sanitized as `/path/to/internal-standards` on purpose — restore the placeholder after every sync.

  This is worse when paired with a known-unfixed vulnerability: an accurate internal map plus a documented weakness is a bigger gift than either alone. If something must be recorded, put it in the (private) bd issue, not in a tracked file. Precedent: vts-luf4 — a docstring documenting an SSRF residual also described the host's network layout.
- **Knowledge capture** is a managed shared rule now — see "Knowledge Capture" in `.ai/managed/shared-engineering-policy.md`. vts specifics: what you'd store via `bd remember` also goes to the Cognee `development_knowledge` dataset via `mcp__claude_ai_Cognee__remember(dataset_name="development_knowledge")`, project-tagged `Project: vts (...)`.
- **Beads guidance lives here, not in `CLAUDE.md`**: `bd setup claude` used to write a `<!-- BEGIN BEADS INTEGRATION -->` block straight into `CLAUDE.md`, which `internal-standards` also generates — two owners, one file, permanent drift (`bd setup claude --check` reported "stale", `standards sync` refused to write). The block was removed with `bd setup claude --remove` and its content moved into this file, which `internal-standards` treats as an authoritative local extension and never regenerates. Do NOT run `bd setup claude` here; it would re-introduce the conflict. The `bd prime` SessionStart/PreCompact hooks in `.claude/settings.json` stay — `--remove` strips those too, so re-add them by hand if you ever run it.
- **Harness-managed `.claude/settings.json` is local-only**: hooks on this machine append a `worktree.bgIsolation` block and per-machine hook entries (background-setup.sh, inbox-notifier.sh) to `.claude/settings.json` every session. The canonical repo version contains only the shared `bd prime` SessionStart/PreCompact hooks. On a fresh clone, run `git update-index --skip-worktree .claude/settings.json` so local mutations stop showing up in `git status` and stop interrupting commits with stash dances. `.claude/scheduled_tasks.lock` and `.claude/settings.json.old` are gitignored runtime state.

# Beads issue tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` for full workflow context and commands — it is the single source of truth for operational commands, and a SessionStart hook runs it automatically.

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Take an issue into work
bd close <id>           # Complete work
```

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists.
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files.
- `.beads/` is gitignored, so issue text is private. That makes it the right place for detail which must not ship in this public repo (see the sensitive-data rule above).

## Session completion

When ending a work session, complete ALL of these. Work is NOT complete until `git push` succeeds.

1. **File issues for remaining work** — anything needing follow-up.
2. **Run quality gates** (if code changed) — tests, linters, builds.
3. **Update issue status** — close finished work, update in-progress items.
4. **Push** — this is mandatory:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** — clear stashes, prune remote branches.
6. **Verify** — all changes committed AND pushed.
7. **Hand off** — provide context for the next session.

Never stop before pushing, and never say "ready to push when you are" — that strands work locally. If push fails, resolve and retry until it succeeds. (This overrides the upstream `bd setup` template's more conservative "report proposed commands unless authorized" default, which does not match how this project works.)

# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED
Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:
- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED
Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:
- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED
WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:
- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)
Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:
- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)
If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)
Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call the `ctx_stats` MCP tool and display the full output verbatim |
| `ctx doctor` | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |

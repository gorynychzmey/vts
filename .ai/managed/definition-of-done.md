<!-- internal-standards:managed -->
# Definition Of Done

## Definition Of Done

- Required checks for the touched area have passed.
- Managed standards files are in sync if the task changes them.
- Relevant documentation or workflow contracts are updated when automation changes.
- The staged change has been reviewed for credentials and internal infrastructure detail (see "Pre-Commit Sensitive Data Review").
- The change is committed and pushed before the task is considered complete.

## Pre-Commit Sensitive Data Review

- Before every commit, review what you are about to publish. This is a check to run, not a disposition to have: read `git diff --staged`, and `git status` for newly tracked files, specifically looking for data that should not leave the machine. Do it even when the change looks purely textual — documentation, comments and tests leak as easily as code.
- Know whether the repository is public before you commit to it, and treat "private today" as "may be public later" — history is not retroactively fixable without a rewrite. When in doubt, assume the contents are world-readable forever.
- Look for these categories:
  - **Credentials**: keys, passwords, tokens, connection strings with embedded credentials, `.env` contents, certificates and private keys.
  - **Internal infrastructure**: real hostnames (including LAN-only names), internal IP addresses, network topology, which services share a network, host filesystem paths that reveal layout, ports, and file modes.
  - **Third-party context**: names of unrelated services, containers or customers that happen to share a host or a tracker. They belong to someone else and are not yours to publish.
  - **Personal data**: names, emails, phone numbers and addresses appearing as sample or test data.
- In fixtures and tests, use reserved placeholders instead of real values: `example.com` / `example.invalid` for hosts, RFC5737 ranges (`192.0.2.0/24`) for addresses, obviously-fake credentials. Assert on the *shape* of a value or message, never on a real hostname — an assertion is a bad reason to publish infrastructure.
- Prefer a generic word to a specific one when it carries the same meaning. "prod" is almost always as informative to the reader as the production host's actual name, and costs nothing.
- Never track files written under an assumption of privacy: issue-tracker exports and backups, pasted logs, database dumps, crash reports, support transcripts. Add them to `.gitignore` at the moment they are created, not after they are noticed in a diff.
- Be strictest when the change also documents a weakness. An accurate internal map published next to a known-unfixed vulnerability is a larger gift to an attacker than either part alone. Security findings belong in the private tracker; a tracked file should describe the code's behavior, not the environment it runs in.
- If sensitive data has already been committed, say so plainly rather than quietly amending. Removing it from the current tree does not remove it from history, and the decision about rewriting history or rotating a leaked credential belongs to the repository owner.

## Task Completion

- When a task is done, bump the version if needed by project rules, commit all changed files with a descriptive message, and push to origin/main. Do this as the final step of every response that completes a task, without waiting for the user to ask.
- Use focused staging; avoid indiscriminate repository-wide staging when a project policy provides narrower guidance.
- Keep one logical change per commit.
- Do not force-push primary branches without explicit approval.

## Test Environment Parity

- Tests must run against the SAME backing services (database, cache, queue, etc.) in CI as in the local development environment. If dev tests hit a real Postgres, CI must too — do not let one environment silently substitute a different engine (e.g. SQLite in-memory) for another.
- A substitution or in-memory fake is allowed ONLY as a deliberate, documented decision for a specific test — not as an accidental default. When you do substitute, say why in the test (a comment) and confirm the substituted behavior still matches the real backend for what the test asserts.
- A test that needs a backend not provisioned in CI is a defect: either provision it in CI, or rewrite the test onto the shared real-backend harness the rest of the suite uses. Never rely on a dependency that only exists in a local virtualenv.
- Before adding a test that opens its own engine/connection, use the project's existing shared test-DB/service harness so environment parity is automatic.

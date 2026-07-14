<!-- internal-standards:managed -->
# Definition Of Done

## Definition Of Done

- Required checks for the touched area have passed.
- Managed standards files are in sync if the task changes them.
- Relevant documentation or workflow contracts are updated when automation changes.
- The change is committed and pushed before the task is considered complete.

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

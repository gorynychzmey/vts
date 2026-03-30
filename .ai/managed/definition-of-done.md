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

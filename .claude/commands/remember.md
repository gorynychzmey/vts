---
description: Save a working command or pattern to project memory so it's used correctly next time
---

The user wants to save a working command, path, or pattern to memory.

1. Read the user's argument (what to remember).
2. Append it to the **"Verified commands"** section of `/path/to/claude-memory/MEMORY.md`.
   - If the section doesn't exist yet, add it.
   - Use a concise format: `- <what>: <exact working form>`
3. Also update `/path/to/vts/.claude/MEMORY.md` or the relevant `.claude/*.md` file if the fact belongs there.
4. Confirm to the user what was saved.

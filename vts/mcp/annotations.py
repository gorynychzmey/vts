"""Read/write hints for MCP tools (vts-ann1).

Clients group tools by `readOnlyHint`, so a user sees "these only look, those
change things" instead of one flat list. Without the hints everything lands in
the same bucket and an assistant cannot tell `delete_prompt` from `list_prompts`.

Three shapes cover this server, and they are shared rather than written out per
tool so the vocabulary cannot drift between domains:

* READ_ONLY — looks, changes nothing.
* MUTATING — creates or updates. Nothing the user had is lost, so a client need
  not interrupt them to ask.
* DESTRUCTIVE — removes something. `destructiveHint` is a client's cue to
  confirm first, which is exactly right for a delete.

`idempotentHint` is set where repeating the call is genuinely harmless: an
update writes the same row twice, a delete of something already gone is a
no-op. It is left unset for creates, which produce a second object.
"""
from __future__ import annotations

from mcp.types import ToolAnnotations

# Looks only.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    # These tools read this deployment's own database, not the wider internet.
    openWorldHint=False,
)

# Creates something new: calling twice makes two of them.
CREATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

# Overwrites a specific thing: calling twice leaves the same result.
UPDATE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Removes something. The one shape a client should confirm before calling.
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

# Starts a pipeline run: it downloads from the open internet, and it occupies a
# GPU for as long as the recording takes. Not destructive — a run can be
# cancelled and nothing existing is lost (the owner's call).
SUBMIT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# Re-sends an already-configured delivery to an external system. Reaches
# outside this deployment, but repeats something the user set up rather than
# destroying anything.
DELIVER = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

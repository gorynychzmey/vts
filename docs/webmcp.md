# WebMCP

VTS exposes its core MCP capability surface to browser-based agents that
implement the [WebMCP imperative API](https://webmachinelearning.github.io/webmcp/).
This complements the server-side MCP endpoint; it does not replace it.

## Tools

| Tool | Existing application capability |
| --- | --- |
| `search_transcripts` | `GET /api/search` |
| `get_recording_transcript` | `GET /api/recordings/{id}/transcript` |
| `list_recordings` | `GET /api/recordings` |
| `list_people` | `GET /api/speakers` |
| `submit_video` | `POST /api/tasks` |
| `delete_task` | `DELETE /api/tasks` |
| `rename_recording` | `PATCH /api/recordings/{id}` |
| `list_prompts`, `create_prompt`, `update_prompt`, `delete_prompt` | `/api/prompts` |
| `list_presets`, `create_preset`, `update_preset`, `delete_preset` | `/api/presets` |
| `get_default_preset`, `set_default_preset` | `/api/me/default_preset` |

The adapter deliberately calls the same HTTP API as the web UI. Consequently,
cookie authentication, development authentication, administrator impersonation,
tenant/owner filtering, and HTTP validation remain enforced by the existing
backend. WebMCP does not receive a separate credential or additional access.
Transcript and speaker data is marked as untrusted content in tool annotations.
Mutating tools use the same names as their server-side MCP counterparts and are
annotated as create/update or consequential destructive operations. Destructive
operations are never disguised as reads, so the browser/agent can require user
confirmation before execution.

Unsupported browsers simply skip registration. A rejected registration (for
example, because the `tools` Permissions Policy is disabled) is visible at
debug level in the browser console and does not interrupt the web application.

## Verify in Microsoft Edge DevTools

1. Use an Edge build that implements WebMCP and enable its WebMCP developer
   preview/experimental feature if the API is not enabled by default.
2. Sign in to VTS and open the application over HTTPS (or localhost).
3. In DevTools Console, evaluate `document.modelContext`. It must be defined.
4. Evaluate `await document.modelContext.getTools()` and confirm that the tools
   above are present and that destructive tools have `consequentialHint: true`.
5. Invoke a tool through the WebMCP DevTools/agent UI, or use
   `document.modelContext.executeTool()` with an item returned by `getTools()`.
6. In the Network panel, confirm the call targets `/api/...`, carries the same
   session as UI calls, and returns only the signed-in user's data. Repeat with
   a recording ID owned by another test user and confirm the existing `404`
   ownership response.

The API is experimental. When testing a new Edge build, use `getTools()` rather
than relying on a particular DevTools panel name or location.

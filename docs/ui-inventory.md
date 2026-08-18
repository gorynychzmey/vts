# UI inventory

<!-- GENERATED FILE — do not edit by hand. Regenerate with `make ui-inventory` (see scripts/gen_ui_inventory.py). -->

Every user-facing capability in VTS: what it acts on, what you can do, which states it moves through, which endpoint serves it, and where it lives in the interface. Rows are derived from the FastAPI route table, `vts/static/index.html`, `vts/static/app.js`, `vts/static/i18n/en.js`, the `StrEnum`s in `vts/db/models.py`, and the Alembic migrations — nothing here is written from memory.

**Counts:** 52 capabilities · 101 routes (74 in the OpenAPI schema, 27 hidden) · 26 MCP tools · 390 English UI strings.

## Capabilities by entity

### Task

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Create from URL | Video URL | -> queued | `POST /api/tasks` | New Task card (`index.html` — `#task-form`, source type “URL”) |
| Create from uploaded file(s) | Video / audio file | -> queued | `GET /api/uploads/config`<br>`POST /api/uploads/init`<br>`PATCH /api/uploads/{upload_id}`<br>`GET /api/uploads/{upload_id}/offset`<br>`POST /api/uploads/{upload_id}/finalize` | New Task card — source type “File”; resumable chunked upload with progress toast |
| Create via multipart (single request) | — | -> queued | `POST /api/tasks/upload` | **No UI** — API/script path only; the browser uses the resumable `/api/uploads/*` flow |
| List, filter and page | Tasks | any | `GET /api/tasks` | Tasks card — `#task-filters` (text, source type, date range) + infinite scroll `#task-sentinel` |
| Inspect one task | About task | any | `GET /api/tasks/{task_id}` | Task card expand + About dialog (`#task-about-dialog`) |
| Rename | Rename task | any | `PATCH /api/tasks/{task_id}` | Task card — inline name editor (`.task-edit-name-btn`) |
| Pause | Pause processing after the current step | queued/running -> paused | `POST /api/tasks/pause` | Task card toolbar |
| Resume | Resume processing from where it stopped | paused/failed/awaiting_input -> queued | `POST /api/tasks/resume` | Task card toolbar |
| Archive (drop media, keep text) | Archive: remove from the list and delete media; transcript and summary are kept | -> archived | `POST /api/tasks/archive` | Task card toolbar (confirm: `confirm.archive`) |
| Delete | Delete the task and all its files — cannot be undone | any -> gone | `DELETE /api/tasks` | Task card toolbar (confirm: `confirm.delete`) |
| Restart summary (full / final only) | Restart summary… | completed -> queued | `POST /api/tasks/{task_id}/restart_summary` | Task card toolbar + `#restart-final-dialog` for the final-only variant |
| Watch live progress | Overall progress | queued/waiting/running | `GET /api/events`<br>`GET /api/tasks/queue-positions`<br>`GET /api/progress-weights` | Task card progress bars (overall + current step); SSE stream, polled queue positions |
| Stage files before upload (add, reorder, remove) | Drop files here or pick them from disk | - | — | New Task card — `#file-drop`; the staging list is client-side, the upload itself uses `/api/uploads/*` |

### Task artefact

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Read raw transcript | Raw transcript | enabled once produced | `GET /api/tasks/{task_id}/transcript` | Task card — Transcript tab |
| Read processed transcript | Processed transcript | enabled once produced | `GET /api/tasks/{task_id}/redacted` | Task card — Processed transcript tab (disabled until ready) |
| Read summary | Summary | enabled once produced | `GET /api/tasks/{task_id}/summary` | Task card — Summary tab, with a per-prompt result selector when >1 prompt ran |
| Read task log | Log | any | `GET /api/tasks/{task_id}/log` | Task card — Log tab |
| Copy / download open tab | Download the open tab's content as a file | tab has content | — | Tab toolbar (`.tab-copy-btn`, `.tab-save-btn`) — client-side, no endpoint |
| Download original media | Download the original media file | media present | `GET /api/tasks/{task_id}/media` | Task card toolbar; hidden once the retention policy expires the file (`tasks.media_expired_badge`) |
| Play media alongside transcript | Open player with transcript | media + transcript present | `GET /player/{task_id}`<br>`GET /api/tasks/{task_id}/transcript-entries` | Standalone player page opened from the task card |
| Fetch a single prompt result | — | result exists | `GET /api/tasks/{task_id}/results/{source}/{ref}` | Backing endpoint for the Summary tab result selector — not a screen of its own |

### Speaker (voice)

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Resolve voices for a task | Resolve voices | awaiting_input -> queued | `GET /api/tasks/{task_id}/speaker-matches`<br>`GET /api/tasks/{task_id}/speaker-previews/{speaker_label}/{index}/audio`<br>`POST /api/tasks/{task_id}/speakers` | `#voice-resolution-dialog` — save, or save & continue the pipeline |
| Browse / create / rename / delete people | Voice registry | - | `GET /api/speakers`<br>`POST /api/speakers`<br>`PATCH /api/speakers/{speaker_id}`<br>`DELETE /api/speakers/{speaker_id}` | `#speaker-registry-dialog` (header menu -> Manage voices) |
| Merge one person into another | Merge into another person | source person removed | `POST /api/speakers/{source_id}/merge` | `#speaker-registry-dialog` — merge action on the selected person |

### Voice sample

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List / play / delete fragments | No voice fragments yet. | - | `GET /api/speakers/{speaker_id}/samples`<br>`GET /api/speakers/samples/{sample_id}/audio`<br>`DELETE /api/speakers/{speaker_id}/samples/{sample_id}` | `#speaker-registry-dialog` — fragment list for the selected person |
| Move a fragment to another person | Move fragment | - | `GET /api/speakers/{speaker_id}/samples/{sample_id}/move-candidates`<br>`POST /api/speakers/{speaker_id}/samples/{sample_id}/move` | `#speaker-picker-dialog` — candidates sorted by similarity or alphabetically |

### Prompt

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List, create, edit, duplicate, delete | Manage prompts | system prompts are read-only (`prompts.manage.system_readonly`) | `GET /api/prompts`<br>`POST /api/prompts`<br>`GET /api/prompts/{prompt_id}`<br>`PATCH /api/prompts/{prompt_id}`<br>`DELETE /api/prompts/{prompt_id}` | `#prompts-dialog` (header menu -> Manage prompts) |
| Read a built-in prompt's text | read-only | system prompts only | `GET /api/prompts/system/{key}/text` | `#prompts-dialog` — read-only body of a system prompt |

### Preset

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List, create, edit, duplicate, delete | Manage presets | system / default badges | `GET /api/presets`<br>`POST /api/presets`<br>`PATCH /api/presets/{preset_id}`<br>`DELETE /api/presets/{preset_id}` | `#presets-dialog` (header menu -> Manage presets) |
| Apply to a new task / save current settings | Save as preset | - | — | New Task card — `#preset-select`, “Save as preset”, and the re-save hint for presets with deleted prompts |
| Set the default preset for new tasks | Use this preset by default for new tasks | - | `GET /api/me/default_preset`<br>`PUT /api/me/default_preset` | `#presets-dialog` — “use by default” toggle |

### Delivery connection

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List, create, edit, delete | Connections | adapter may be missing (`delivery.adapter_missing`) | `GET /api/delivery-credentials`<br>`POST /api/delivery-credentials`<br>`GET /api/delivery-credentials/{credential_id}`<br>`PUT /api/delivery-credentials/{credential_id}`<br>`DELETE /api/delivery-credentials/{credential_id}` | `#delivery-dialog` — Connections tab |
| Test a connection | Test connection | - | `POST /api/delivery-credentials/{credential_id}/check` | `#delivery-dialog` — “Test connection”, with typed outcomes (unreachable / unauthorized / not_found / unexpected_response / timeout) |

### Delivery destination

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List, create, edit, delete | Destinations | - | `GET /api/delivery-targets`<br>`POST /api/delivery-targets`<br>`GET /api/delivery-targets/{target_id}`<br>`PUT /api/delivery-targets/{target_id}`<br>`DELETE /api/delivery-targets/{target_id}` | `#delivery-dialog` — Destinations tab |
| Populate adapter-defined dropdowns | Could not load the list — the system is unavailable. | - | `GET /api/delivery-credentials/{credential_id}/options/{field}` | `#delivery-dialog` — dynamic fields (e.g. collection picker) |

### Delivery

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Discover installed adapters | No delivery plugins are installed, so there is nothing to configure yet. | - | `GET /api/delivery-adapters` | `#delivery-dialog` — drives the “no plugins installed” empty state |
| Choose destinations for a task | Deliver to | - | — | New Task card — `#delivery-select-field` (hidden when no adapters are installed) |
| Review attempts / retry | — | DeliveryStatus | `GET /api/tasks/{task_id}/deliveries`<br>`POST /api/tasks/{task_id}/deliveries/retry` | **No UI** — MCP (`get_delivery_status`, `retry_delivery`) and API only |

### Session

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Log in / log out | Log out | - | `GET /auth/login`<br>`GET /auth/callback`<br>`POST /auth/logout` | OIDC redirect; log-out button in the header |
| See who you are acting as | Authenticated: | admin suffix when applicable | `GET /api/me` | Header context line |
| Act as another user (admin) | Choose a user to act as | - | `GET /api/admin/users` | Header — user selector, admins only |

### API token

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| List, create, revoke | API tokens | - | `GET /api/me/tokens`<br>`POST /api/me/tokens`<br>`DELETE /api/me/tokens/{token_id}` | `#tokens-dialog` (header menu -> Manage API tokens); raw value shown once |

### Push subscription

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Enable / disable browser notifications | Enable browser notifications when tasks finish | - | `GET /api/push/config`<br>`GET /api/push/status`<br>`POST /api/push/subscribe`<br>`POST /api/push/unsubscribe` | Header menu toggle (hidden when VAPID is not configured) |

### App

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Receive a share from the OS | — | - | `GET /share`<br>`POST /share` | PWA share target — hands the shared URL to the New Task form. `/_share_inbox` in `app.js` is a service-worker cache key, not a server route: `sw.js` intercepts it and it never reaches the API |
| Switch theme (system / light / dark) | Theme: system | system / light / dark | — | Header — `#theme-toggle-btn`; client-side only, the choice is not stored server-side |
| Switch interface language | Interface language | en / ru / de | — | Header — `#locale-toggle-btn` cycles en/ru/de; client-side only, no endpoint |
| Show version / detect a new build | Version: | - | `GET /api/version` | Header version label; polled to prompt a reload |
| Read status/step vocabulary | — | - | `GET /api/status-config` | No screen — drives status chips and step names client-side |
| Install as a PWA / work offline | — | - | `GET /manifest.webmanifest`<br>`GET /sw.js` | Browser install prompt; service worker |
| Read the privacy notice | — | - | `GET /privacy` | `/privacy` page |

### Ops

| Action | Label (en) | States | Endpoint(s) | Screen |
| --- | --- | --- | --- | --- |
| Health check | — | - | `GET /healthz` | **No UI** — probes only |
| Browse the API docs | — | - | `GET /docs`<br>`GET /redoc`<br>`GET /openapi.json` | **No UI in the app** — FastAPI's own pages |

## Reachable without a screen

These capabilities exist in the API but have no control anywhere in the web UI. They are reached from scripts, the MCP server, or the browser itself.

| Entity | Action | Endpoint(s) |
| --- | --- | --- |
| Task | Create via multipart (single request) | `POST /api/tasks/upload` |
| Delivery | Review attempts / retry | `GET /api/tasks/{task_id}/deliveries`<br>`POST /api/tasks/{task_id}/deliveries/retry` |
| App | Read status/step vocabulary | `GET /api/status-config` |
| Ops | Health check | `GET /healthz` |
| Ops | Browse the API docs | `GET /docs`<br>`GET /redoc`<br>`GET /openapi.json` |

## States

### `TaskStatus`

| Value | Label (en) |
| --- | --- |
| `queued` | queued |
| `running` | running |
| `waiting` | waiting |
| `paused` | paused |
| `completed` | completed |
| `archived` | archived |
| `failed` | failed |
| `canceled` | canceled |
| `awaiting_input` | needs review |

### `StepStatus`

`pending`, `running`, `completed`, `failed`, `skipped`

### `DeliveryStatus`

`pending`, `delivering`, `delivered`, `dead`, `waiting_adapter`

### Pipeline steps

Step names a task moves through, in the order the UI declares them.

| Step | Label (en) |
| --- | --- |
| `download` | Media download |
| `extract_audio` | Audio extraction |
| `trim_initial_silence` | Initial silence trim |
| `segment_audio` | Audio segmentation |
| `detect_language` | Language detection |
| `transcribe_segments` | Segment transcription |
| `diarize` | Speaker diarization |
| `merge_transcript` | Transcript merge |
| `prepare_llama_model` | LLM warm-up |
| `match_speakers` | Speaker matching |
| `prepare_summary_chunks` | Summary chunking |
| `summarize_windows` | Window summaries |
| `pack_window_notes` | Notes compaction |
| `summarize_final` | Final summary |

## Screens

The web UI is a single page (`vts/static/index.html`): one New Task card, one Tasks list, and a set of `<dialog>` overlays opened from the header menu or a task card.

| Dialog id | Title (en) |
| --- | --- |
| `#tokens-dialog` | API tokens |
| `#prompts-dialog` | Manage prompts |
| `#presets-dialog` | Manage presets |
| `#delivery-dialog` | Manage delivery |
| `#restart-final-dialog` | Restart final with prompts |
| `#task-about-dialog` | — |
| `#speaker-registry-dialog` | Voice registry |
| `#voice-resolution-dialog` | Resolve voices |
| `#speaker-picker-dialog` | Move fragment to a person (set at runtime) |

## MCP tools

`vts/mcp/tools_registry/` exposes the same capabilities to agents. Tools with no matching UI control are the practical reason the “no screen” list above is not a gap.

`submit_video`, `list_tasks`, `get_status`, `get_transcript`, `get_prompt_result`, `wait_for_task`, `list_prompts`, `create_prompt`, `update_prompt`, `delete_prompt`, `list_presets`, `create_preset`, `update_preset`, `delete_preset`, `get_default_preset`, `set_default_preset`, `list_delivery_targets`, `list_delivery_credentials`, `create_delivery_credential`, `update_delivery_credential`, `delete_delivery_credential`, `create_delivery_target`, `update_delivery_target`, `delete_delivery_target`, `get_delivery_status`, `retry_delivery`

## Route coverage

Machine-facing routes — the OAuth authorisation-server endpoints that MCP clients drive, plus the SPA entry point. No user ever navigates to them directly, so they carry no capability row:

- `GET /`
- `GET /.well-known/oauth-authorization-server`
- `GET /.well-known/oauth-protected-resource/mcp`
- `GET /authorize`
- `POST /authorize`
- `GET /consent`
- `POST /consent`
- `GET /docs/oauth2-redirect`
- `GET /mcp/auth/callback`
- `POST /register`
- `POST /token`

Routes not claimed by any capability above (each is a documentation gap):

- `GET /api/tasks/count`

Frontend call sites with no matching route (would be a bug):

- none

## Schema history

Migrations that introduced or changed the entities above.

| Revision | Change |
| --- | --- |
| `0001_initial` | Initial schema. |
| `0002_user_preferred_ytdlp_client` | Add per-user preferred yt-dlp player client. |
| `0003_task_status_archived` | Add archived task status. |
| `0004_task_source_title` | Add source_title to tasks. |
| `0005_task_summary_progress` | Add summary_progress to tasks. |
| `0006_drop_asr_words` | Drop asr_words table. |
| `0007_task_donor_index` | Add index on tasks(source_url, status) for donor lookup. |
| `0008_push_subscriptions` | Add push_subscriptions table for Web Push notifications. |
| `0009_api_tokens` | Add api_tokens table for personal API tokens (Bearer auth). |
| `0010_prompts` | Add prompts table for user-defined custom prompts (VOS-63). |
| `0011_presets` | Add presets table and users.default_preset (vts-hp7). |
| `0012_user_step_weights` | Add user_step_weights table (vts-8cm). |
| `0013_task_status_waiting` | Add waiting task status. |
| `0014_pgvector_extension` | Enable pgvector. |
| `0015_speakers` | Speaker registry (vts-80i). |
| `0016_voice_samples` | Voice samples with pgvector embeddings (vts-80i). |
| `0017_match_decisions` | Match decisions — record every human match/reject/override for calibration (vts-80i). |
| `0018_task_status_awaiting_input` | Add awaiting_input task status and Task.awaiting_step (vts-80i). |
| `0019_match_decision_is_noise` | Add MatchDecision.is_noise (vts-552). |
| `0020_delivery_targets` | delivery_targets |
| `0021_delivery_attempts` | delivery_attempts |
| `0022_delivery_credentials` | delivery_credentials: split the connection out of delivery_targets (vts-929) |
| `0023_delivery_variant_width` | widen delivery_attempts.variant for prompt refs (vts-as1i) |


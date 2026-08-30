#!/usr/bin/env python3
"""Generate docs/ui-inventory.md from the code, not from memory.

Every row in the generated table is derived from a source that ships with the
repo, so the inventory cannot drift into describing features that do not exist:

* **API endpoints** — introspected from the live FastAPI app (``vts.api.main``),
  including routes hidden from the OpenAPI schema (``include_in_schema=False``).
* **Screens** — parsed out of ``vts/static/index.html`` (dialogs, cards, tabs)
  and matched against the ``fetch()`` call sites in ``vts/static/app.js``.
* **Labels** — read from ``vts/static/i18n/en.js``; a capability with no i18n
  key is reported as such rather than given an invented name.
* **States** — read from the ``StrEnum`` classes in ``vts/db/models.py``.
* **Schema history** — read from the ``alembic/versions/*.py`` docstrings.
* **MCP tools** — read from the ``@mcp.tool(name=...)`` decorators.

Run via ``make ui-inventory`` or ``python scripts/gen_ui_inventory.py``.
Use ``--check`` in CI to fail when the committed doc is stale.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "vts" / "static"
OUT = ROOT / "docs" / "ui-inventory.md"

# Paths app.js fetches that sw.js answers from cache — they never hit the API,
# so they are not orphaned routes.
SW_VIRTUAL_PATHS = {"/_share_inbox"}

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def load_routes() -> list[dict]:
    """Introspect the real FastAPI app so hidden routes are not missed.

    ``/auth/*`` is mounted only when ``oauth_enabled`` is set, so the flag is
    forced on here — otherwise the inventory would silently omit login/logout
    on any machine whose ``.env`` has OAuth disabled.
    """
    import os

    sys.path.insert(0, str(ROOT))
    os.environ["VTS_OAUTH_ENABLED"] = "true"
    os.environ["VTS_OAUTH_CLIENT_ID"] = "ui-inventory"
    os.environ["VTS_OAUTH_CLIENT_SECRET"] = "ui-inventory"
    # Enabling OAuth turns on session middleware, which otherwise reads the
    # deployed secret file (root-owned in production). Keep introspection
    # hermetic: an inline throwaway secret, and no secret-file lookup.
    os.environ["VTS_SESSION_SECRET"] = "ui-inventory-generator-not-a-real-secret"
    os.environ["VTS_SESSION_SECRET_FILE"] = ""
    # oauth_enabled asserts a public base URL exists (vts/mcp/server.py).
    os.environ.setdefault("VTS_PUBLIC_BASE_URL", "https://ui-inventory.invalid")

    from vts.api.main import app  # noqa: PLC0415  (import cost is the point)

    routes = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue  # Mounts (/mcp, /static) carry no method set.
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            routes.append(
                {
                    "method": method,
                    "path": path,
                    "in_schema": bool(getattr(route, "include_in_schema", False)),
                    "tags": list(getattr(route, "tags", []) or []),
                    "name": getattr(route, "name", ""),
                }
            )
    return sorted(routes, key=lambda r: (r["path"], r["method"]))


def load_i18n() -> dict[str, str]:
    """Flat key -> English label map from the i18n bundle."""
    src = (STATIC / "i18n" / "en.js").read_text(encoding="utf-8")
    pairs = re.findall(r'"([a-z][\w.]*)":\s*"((?:[^"\\]|\\.)*)"', src)
    return {k: v.replace('\\"', '"') for k, v in pairs}


def load_statuses() -> dict[str, list[str]]:
    """Every StrEnum in vts/db/models.py — parsed, never executed."""
    tree = ast.parse((ROOT / "vts" / "db" / "models.py").read_text(encoding="utf-8"))
    enums: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(getattr(b, "id", "") == "StrEnum" for b in node.bases):
            continue
        values = [
            stmt.value.value
            for stmt in node.body
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
        ]
        if values:
            enums[node.name] = values
    return enums


def load_migrations() -> list[tuple[str, str]]:
    """(revision file stem, docstring first line) for each migration."""
    out = []
    for path in sorted((ROOT / "alembic" / "versions").glob("[0-9]*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        out.append((path.stem, doc.strip().splitlines()[0] if doc else ""))
    return out


def load_mcp_tools() -> list[str]:
    """Tool names from the per-domain registration modules.

    They lived in `vts/mcp/server.py` until the tool bodies moved into
    `vts/mcp/tools_registry/`; `server.py` now only calls `register(mcp)` per
    domain and declares no tools of its own. Modules are read in the order
    `server.py` registers them so the generated list stays stable.
    """
    registry = ROOT / "vts" / "mcp" / "tools_registry"
    server_src = (ROOT / "vts" / "mcp" / "server.py").read_text(encoding="utf-8")
    order = re.search(r"for domain in \(([^)]*)\)", server_src)
    domains = (
        [d.strip() for d in order.group(1).split(",") if d.strip()]
        if order
        else sorted(p.stem for p in registry.glob("*.py") if p.stem != "__init__")
    )
    names: list[str] = []
    for domain in domains:
        src = (registry / f"{domain}.py").read_text(encoding="utf-8")
        names += re.findall(r'@mcp\.tool\(name="([a-z_]+)"', src)
    if not names:
        raise SystemExit("ui-inventory: found no MCP tools — has the registry moved?")
    return names


def load_frontend_calls() -> set[str]:
    """Normalised API paths that app.js actually calls.

    Template placeholders (``${...}``) collapse to ``{}`` so a call site can be
    matched against a route pattern like ``/api/tasks/{task_id}``.
    """
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    raw = re.findall(r"""["'`](/(?:api|auth|share|player|_share_inbox)[^"'`\s]*)""", src)
    calls = set()
    for item in raw:
        path = re.sub(r"\$\{[^}]*\}", "{}", item)
        path = path.split("?")[0].rstrip("/") or "/"
        calls.add(path)
    return calls


def route_pattern(path: str) -> str:
    """/api/tasks/{task_id}/log -> /api/tasks/{}/log, for call-site matching."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def call_matches(call: str, patterns: set[str]) -> bool:
    """Does a normalised app.js call site correspond to a real route?

    Beyond exact equality this tolerates two shapes that are not bugs:

    * a literal where the route has a parameter — ``.../0/audio`` against
      ``.../{index}/audio``;
    * a fully dynamic trailing segment — ``/api/tasks/{}/{}`` is built from a
      variable endpoint name and can resolve to several real routes.
    """
    if call in patterns:
        return True
    call_parts = call.strip("/").split("/")
    for pattern in patterns:
        pat_parts = pattern.strip("/").split("/")
        if len(pat_parts) != len(call_parts):
            continue
        if all(p == c or p == "{}" or c == "{}" for p, c in zip(pat_parts, call_parts)):
            return True
    return False


def load_screens(i18n: dict[str, str]) -> dict[str, str]:
    """Dialog ids in index.html mapped to their rendered English title.

    The title is taken from the dialog's own ``<h2>``: either its ``data-i18n``
    key or, when the heading is filled in at runtime, its literal fallback text.
    Scanning for the first ``data-i18n`` anywhere in the dialog would pick up an
    unrelated control instead (``#speaker-picker-dialog`` would read
    "By similarity", a sort option).
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    screens = {}
    for match in re.finditer(r'<dialog id="([\w-]+)"', html):
        tail = html[match.end() : match.end() + 1200]
        heading = re.search(r"<h2\b([^>]*)>(.*?)</h2>", tail, re.S)
        title = "—"
        if heading:
            attrs, text = heading.group(1), heading.group(2)
            key = re.search(r'data-i18n="([\w.]+)"', attrs)
            if key:
                title = i18n.get(key.group(1), "—")
            else:
                stripped = re.sub(r"<[^>]+>", "", text).strip()
                if stripped:
                    title = f"{stripped} (set at runtime)"
        screens[match.group(1)] = title
    return screens


# --------------------------------------------------------------------------
# Capability model
#
# A capability is a user-facing thing someone can do. It is declared here, but
# every field is *verified* against the loaded sources before it is written out:
# unknown endpoints and unknown i18n keys become loud errors, so this table
# cannot describe a feature the code does not have.
# --------------------------------------------------------------------------

Cap = dict

CAPABILITIES: list[Cap] = [
    # ---- Task ----
    {
        "entity": "Task",
        "action": "Create from URL",
        "label": "new_task.url_label",
        "endpoints": ["POST /api/tasks"],
        "screen": "New Task card (`index.html` — `#task-form`, source type “URL”)",
        "states": "-> queued",
    },
    {
        "entity": "Task",
        "action": "Create from uploaded file(s)",
        "label": "new_task.file_label",
        "endpoints": [
            "GET /api/uploads/config",
            "POST /api/uploads/init",
            "PATCH /api/uploads/{upload_id}",
            "GET /api/uploads/{upload_id}/offset",
            "POST /api/uploads/{upload_id}/finalize",
        ],
        "screen": "New Task card — source type “File”; resumable chunked upload with progress toast",
        "states": "-> queued",
    },
    {
        "entity": "Task",
        "action": "Create via multipart (single request)",
        "label": None,
        "endpoints": ["POST /api/tasks/upload"],
        "screen": "**No UI** — API/script path only; the browser uses the resumable `/api/uploads/*` flow",
        "states": "-> queued",
    },
    {
        "entity": "Task",
        "action": "List, filter and page",
        "label": "tasks.title",
        "endpoints": ["GET /api/tasks"],
        "screen": "Tasks card — `#task-filters` (text, source type, date range) + infinite scroll `#task-sentinel`",
        "states": "any",
    },
    {
        "entity": "Task",
        "action": "Inspect one task",
        "label": "about.title",
        "endpoints": ["GET /api/tasks/{task_id}"],
        "screen": "Task card expand + About dialog (`#task-about-dialog`)",
        "states": "any",
    },
    {
        "entity": "Task",
        "action": "Rename",
        "label": "action.edit_name",
        "endpoints": ["PATCH /api/tasks/{task_id}"],
        "screen": "Task card — inline name editor (`.task-edit-name-btn`)",
        "states": "any",
    },
    {
        "entity": "Task",
        "action": "Pause",
        "label": "action.pause",
        "endpoints": ["POST /api/tasks/pause"],
        "screen": "Task card toolbar",
        "states": "queued/running -> paused",
    },
    {
        "entity": "Task",
        "action": "Resume",
        "label": "action.resume",
        "endpoints": ["POST /api/tasks/resume"],
        "screen": "Task card toolbar",
        "states": "paused/failed/awaiting_input -> queued",
    },
    {
        "entity": "Task",
        "action": "Archive (drop media, keep text)",
        "label": "action.archive",
        "endpoints": ["POST /api/tasks/archive"],
        "screen": "Task card toolbar (confirm: `confirm.archive`)",
        "states": "-> archived",
    },
    {
        "entity": "Task",
        "action": "Delete",
        "label": "action.delete",
        "endpoints": ["DELETE /api/tasks"],
        "screen": "Task card toolbar (confirm: `confirm.delete`)",
        "states": "any -> gone",
    },
    {
        "entity": "Task",
        "action": "Restart summary (full / final only)",
        "label": "action.restart_summary",
        "endpoints": ["POST /api/tasks/{task_id}/restart_summary"],
        "screen": "Task card toolbar + `#restart-final-dialog` for the final-only variant",
        "states": "completed -> queued",
    },
    {
        "entity": "Task",
        "action": "Watch live progress",
        "label": "progress.overall",
        "endpoints": [
            "GET /api/events",
            "GET /api/tasks/queue-positions",
            "GET /api/progress-weights",
        ],
        "screen": "Task card progress bars (overall + current step); SSE stream, polled queue positions",
        "states": "queued/waiting/running",
    },
    # ---- Task artefacts ----
    {
        "entity": "Task artefact",
        "action": "Read raw transcript",
        "label": "tab.transcript",
        "endpoints": ["GET /api/tasks/{task_id}/transcript"],
        "screen": "Task card — Transcript tab",
        "states": "enabled once produced",
    },
    # ---- Knowledge library ----
    {
        "entity": "Recording",
        "action": "Browse the library",
        "label": "library.open",
        "endpoints": ["GET /api/recordings"],
        "screen": "Library tab on the main screen: same cards as Tasks, minus progress/status/log/restart",
        "states": "one recording per task, outliving it; a detached one says so",
    },
    {
        "entity": "Recording",
        "action": "Read a recording's transcript",
        "label": "library.show_context",
        "endpoints": ["GET /api/recordings/{recording_id}/transcript"],
        "screen": "Library tab — 'Show in transcript' on a search hit",
        "states": "works after the originating task is deleted; windowed with around_sec",
    },
    {
        "entity": "Recording",
        "action": "Rename a recording",
        "label": "library.rename",
        "endpoints": ["PATCH /api/recordings/{recording_id}"],
        "screen": "Library tab — recording card menu",
        "states": "a chosen name stops following the task; clearing it resumes",
    },
    {
        "entity": "Recording",
        "action": "Search the corpus",
        "label": "",
        "endpoints": ["GET /api/search"],
        "screen": "API and the search_transcripts MCP tool (no page yet)",
        "states": "returns nothing below the relevance threshold, never the nearest passages",
    },
    {
        "entity": "Recording",
        "action": "Open a recording",
        "label": "library.title",
        "endpoints": ["GET /api/recordings/{recording_id}"],
        "screen": "Library tab — recording card",
        "states": "owner-scoped, 404 otherwise",
    },
    {
        "entity": "Task artefact",
        "action": "Read raw transcript as subtitles",
        "label": "action.subtitles",
        "endpoints": ["GET /api/tasks/{task_id}/subtitles"],
        "screen": "Task card — Transcript tab, subtitles toggle in the tab actions",
        "states": "enabled once produced; works with or without diarization",
    },
    {
        "entity": "Task artefact",
        "action": "Share a result",
        "label": "action.share",
        "endpoints": [],
        "screen": "Task card — share button in the tab actions, opens #share-dialog",
        "states": "offers every ready artefact except the log",
    },
    {
        "entity": "Task artefact",
        "action": "Read processed transcript",
        "label": "tab.redacted",
        "endpoints": ["GET /api/tasks/{task_id}/redacted"],
        "screen": "Task card — Processed transcript tab (disabled until ready)",
        "states": "enabled once produced",
    },
    {
        "entity": "Task artefact",
        "action": "Read summary",
        "label": "tab.summary",
        "endpoints": ["GET /api/tasks/{task_id}/summary"],
        "screen": "Task card — Summary tab, with a per-prompt result selector when >1 prompt ran",
        "states": "enabled once produced",
    },
    {
        "entity": "Task artefact",
        "action": "Read task log",
        "label": "tab.log",
        "endpoints": ["GET /api/tasks/{task_id}/log"],
        "screen": "Task card — Log tab",
        "states": "any",
    },
    {
        "entity": "Task artefact",
        "action": "Copy / download open tab",
        "label": "action.save_tab",
        "endpoints": [],
        "screen": "Tab toolbar (`.tab-copy-btn`, `.tab-save-btn`) — client-side, no endpoint",
        "states": "tab has content",
    },
    {
        "entity": "Task artefact",
        "action": "Download original media",
        "label": "action.download_media",
        "endpoints": ["GET /api/tasks/{task_id}/media"],
        "screen": "Task card toolbar; hidden once the retention policy expires the file (`tasks.media_expired_badge`)",
        "states": "media present",
    },
    {
        "entity": "Task artefact",
        "action": "Play media alongside transcript",
        "label": "tasks.open_player",
        "endpoints": ["GET /player/{task_id}", "GET /api/tasks/{task_id}/transcript-entries"],
        "screen": "Standalone player page opened from the task card",
        "states": "media + transcript present",
    },
    {
        "entity": "Task artefact",
        "action": "Fetch a single prompt result",
        "label": None,
        "endpoints": ["GET /api/tasks/{task_id}/results/{source}/{ref}"],
        "screen": "Backing endpoint for the Summary tab result selector — not a screen of its own",
        "states": "result exists",
    },
    # ---- Speakers ----
    {
        "entity": "Speaker (voice)",
        "action": "Resolve voices for a task",
        "label": "voices.dialog.title",
        "endpoints": [
            "GET /api/tasks/{task_id}/speaker-matches",
            "GET /api/tasks/{task_id}/speaker-previews/{speaker_label}/{index}/audio",
            "POST /api/tasks/{task_id}/speakers",
        ],
        "screen": "`#voice-resolution-dialog` — save, or save & continue the pipeline",
        "states": "awaiting_input -> queued",
    },
    {
        "entity": "Speaker (voice)",
        "action": "Browse / create / rename / delete people",
        "label": "speakers.registry.title",
        "endpoints": [
            "GET /api/speakers",
            "POST /api/speakers",
            "PATCH /api/speakers/{speaker_id}",
            "DELETE /api/speakers/{speaker_id}",
        ],
        "screen": "`#speaker-registry-dialog` (header menu -> Manage voices)",
        "states": "-",
    },
    {
        "entity": "Speaker (voice)",
        "action": "Merge one person into another",
        "label": "speakers.registry.merge",
        "endpoints": ["POST /api/speakers/{source_id}/merge"],
        "screen": "`#speaker-registry-dialog` — merge action on the selected person",
        "states": "source person removed",
    },
    {
        "entity": "Voice sample",
        "action": "List / play / delete fragments",
        "label": "speakers.registry.samples_empty",
        "endpoints": [
            "GET /api/speakers/{speaker_id}/samples",
            "GET /api/speakers/samples/{sample_id}/audio",
            "DELETE /api/speakers/{speaker_id}/samples/{sample_id}",
        ],
        "screen": "`#speaker-registry-dialog` — fragment list for the selected person",
        "states": "-",
    },
    {
        "entity": "Voice sample",
        "action": "Move a fragment to another person",
        "label": "speakers.registry.move_sample",
        "endpoints": [
            "GET /api/speakers/{speaker_id}/samples/{sample_id}/move-candidates",
            "POST /api/speakers/{speaker_id}/samples/{sample_id}/move",
        ],
        "screen": "`#speaker-picker-dialog` — candidates sorted by similarity or alphabetically",
        "states": "-",
    },
    # ---- Prompts / presets ----
    {
        "entity": "Prompt",
        "action": "List, create, edit, duplicate, delete",
        "label": "prompts.manage.title",
        "endpoints": [
            "GET /api/prompts",
            "POST /api/prompts",
            "GET /api/prompts/{prompt_id}",
            "PATCH /api/prompts/{prompt_id}",
            "DELETE /api/prompts/{prompt_id}",
        ],
        "screen": "`#prompts-dialog` (header menu -> Manage prompts)",
        "states": "system prompts are read-only (`prompts.manage.system_readonly`)",
    },
    {
        "entity": "Prompt",
        "action": "Read a built-in prompt's text",
        "label": "prompts.manage.system_readonly",
        "endpoints": ["GET /api/prompts/system/{key}/text"],
        "screen": "`#prompts-dialog` — read-only body of a system prompt",
        "states": "system prompts only",
    },
    {
        "entity": "Preset",
        "action": "List, create, edit, duplicate, delete",
        "label": "preset.manage.title",
        "endpoints": [
            "GET /api/presets",
            "POST /api/presets",
            "PATCH /api/presets/{preset_id}",
            "DELETE /api/presets/{preset_id}",
        ],
        "screen": "`#presets-dialog` (header menu -> Manage presets)",
        "states": "system / default badges",
    },
    {
        "entity": "Preset",
        "action": "Apply to a new task / save current settings",
        "label": "preset.save_as",
        "endpoints": [],
        "screen": "New Task card — `#preset-select`, “Save as preset”, and the re-save hint for presets with deleted prompts",
        "states": "-",
    },
    {
        "entity": "Preset",
        "action": "Set the default preset for new tasks",
        "label": "preset.manage.make_default",
        "endpoints": ["GET /api/me/default_preset", "PUT /api/me/default_preset"],
        "screen": "`#presets-dialog` — “use by default” toggle",
        "states": "-",
    },
    # ---- Delivery ----
    {
        "entity": "Delivery connection",
        "action": "List, create, edit, delete",
        "label": "delivery.credentials.title",
        "endpoints": [
            "GET /api/delivery-credentials",
            "POST /api/delivery-credentials",
            "GET /api/delivery-credentials/{credential_id}",
            "PUT /api/delivery-credentials/{credential_id}",
            "DELETE /api/delivery-credentials/{credential_id}",
        ],
        "screen": "`#delivery-dialog` — Connections tab",
        "states": "adapter may be missing (`delivery.adapter_missing`)",
    },
    {
        "entity": "Delivery connection",
        "action": "Test a connection",
        "label": "delivery.check.button",
        "endpoints": ["POST /api/delivery-credentials/{credential_id}/check"],
        "screen": "`#delivery-dialog` — “Test connection”, with typed outcomes (unreachable / unauthorized / not_found / unexpected_response / timeout)",
        "states": "-",
    },
    {
        "entity": "Delivery destination",
        "action": "List, create, edit, delete",
        "label": "delivery.targets.title",
        "endpoints": [
            "GET /api/delivery-targets",
            "POST /api/delivery-targets",
            "GET /api/delivery-targets/{target_id}",
            "PUT /api/delivery-targets/{target_id}",
            "DELETE /api/delivery-targets/{target_id}",
        ],
        "screen": "`#delivery-dialog` — Destinations tab",
        "states": "-",
    },
    {
        "entity": "Delivery destination",
        "action": "Populate adapter-defined dropdowns",
        "label": "delivery.options.unavailable",
        "endpoints": ["GET /api/delivery-credentials/{credential_id}/options/{field}"],
        "screen": "`#delivery-dialog` — dynamic fields (e.g. collection picker)",
        "states": "-",
    },
    {
        "entity": "Delivery",
        "action": "Discover installed adapters",
        "label": "delivery.no_adapters",
        "endpoints": ["GET /api/delivery-adapters"],
        "screen": "`#delivery-dialog` — drives the “no plugins installed” empty state",
        "states": "-",
    },
    {
        "entity": "Delivery",
        "action": "Choose destinations for a task",
        "label": "new_task.delivery",
        "endpoints": [],
        "screen": "New Task card — `#delivery-select-field` (hidden when no adapters are installed)",
        "states": "-",
    },
    {
        "entity": "Delivery",
        "action": "Review attempts / retry",
        "label": None,
        "endpoints": [
            "GET /api/tasks/{task_id}/deliveries",
            "POST /api/tasks/{task_id}/deliveries/retry",
        ],
        "screen": "**No UI** — MCP (`get_delivery_status`, `retry_delivery`) and API only",
        "states": "DeliveryStatus",
    },
    # ---- Account ----
    {
        "entity": "Session",
        "action": "Log in / log out",
        "label": "action.logout",
        "endpoints": ["GET /auth/login", "GET /auth/callback", "POST /auth/logout"],
        "screen": "OIDC redirect; log-out button in the header",
        "states": "-",
    },
    {
        "entity": "Session",
        "action": "See who you are acting as",
        "label": "context.authenticated",
        "endpoints": ["GET /api/me"],
        "screen": "Header context line",
        "states": "admin suffix when applicable",
    },
    {
        "entity": "Session",
        "action": "Act as another user (admin)",
        "label": "admin.switch_user",
        "endpoints": ["GET /api/admin/users"],
        "screen": "Header — user selector, admins only",
        "states": "-",
    },
    {
        "entity": "API token",
        "action": "List, create, revoke",
        "label": "tokens.title",
        "endpoints": [
            "GET /api/me/tokens",
            "POST /api/me/tokens",
            "DELETE /api/me/tokens/{token_id}",
        ],
        "screen": "`#tokens-dialog` (header menu -> Manage API tokens); raw value shown once",
        "states": "-",
    },
    {
        "entity": "Push subscription",
        "action": "Enable / disable browser notifications",
        "label": "action.enable_notifications",
        "endpoints": [
            "GET /api/push/config",
            "GET /api/push/status",
            "POST /api/push/subscribe",
            "POST /api/push/unsubscribe",
        ],
        "screen": "Header menu toggle (hidden when VAPID is not configured)",
        "states": "-",
    },
    # ---- App-level ----
    {
        "entity": "App",
        "action": "Receive a share from the OS",
        "label": None,
        "endpoints": ["GET /share", "POST /share"],
        "screen": (
            "PWA share target — hands the shared URL to the New Task form. "
            "`/_share_inbox` in `app.js` is a service-worker cache key, not a "
            "server route: `sw.js` intercepts it and it never reaches the API"
        ),
        "states": "-",
    },
    {
        "entity": "App",
        "action": "Switch theme (system / light / dark)",
        "label": "theme.system",
        "endpoints": [],
        "screen": "Header — `#theme-toggle-btn`; client-side only, the choice is not stored server-side",
        "states": "system / light / dark",
    },
    {
        "entity": "App",
        "action": "Switch interface language",
        # The visible label is an endonym (English / Русский / Deutsch) and is
        # deliberately NOT translated — someone looking for German scans for
        # "Deutsch", not "Немецкий". `header.language` is the button's
        # accessible name, which is translated.
        "label": "header.language",
        "endpoints": [],
        "screen": "Header — `#locale-toggle-btn` cycles en/ru/de; client-side only, no endpoint",
        "states": "en / ru / de",
    },
    {
        "entity": "Task",
        "action": "Stage files before upload (add, reorder, remove)",
        "label": "new_task.file_drop_title",
        "endpoints": [],
        "screen": "New Task card — `#file-drop`; the staging list is client-side, the upload itself uses `/api/uploads/*`",
        "states": "-",
    },
    {
        "entity": "App",
        "action": "Show version / detect a new build",
        "label": "header.version",
        "endpoints": ["GET /api/version"],
        "screen": "Header version label; polled to prompt a reload",
        "states": "-",
    },
    {
        "entity": "App",
        "action": "Read status/step vocabulary",
        "label": None,
        "endpoints": ["GET /api/status-config"],
        "screen": "No screen — drives status chips and step names client-side",
        "states": "-",
    },
    {
        "entity": "App",
        "action": "Install as a PWA / work offline",
        "label": None,
        "endpoints": ["GET /manifest.webmanifest", "GET /sw.js"],
        "screen": "Browser install prompt; service worker",
        "states": "-",
    },
    {
        "entity": "App",
        "action": "Read the privacy notice",
        "label": None,
        "endpoints": ["GET /privacy"],
        "screen": "`/privacy` page",
        "states": "-",
    },
    {
        "entity": "Ops",
        "action": "Health check",
        "label": None,
        "endpoints": ["GET /healthz"],
        "screen": "**No UI** — probes only",
        "states": "-",
    },
    {
        "entity": "Ops",
        "action": "Browse the API docs",
        "label": None,
        "endpoints": ["GET /docs", "GET /redoc", "GET /openapi.json"],
        "screen": "**No UI in the app** — FastAPI's own pages",
        "states": "-",
    },
]


# --------------------------------------------------------------------------
# Verification — the guard against invented features
# --------------------------------------------------------------------------


def verify(caps: list[Cap], routes: list[dict], i18n: dict[str, str]) -> list[str]:
    known = {f"{r['method']} {r['path']}" for r in routes}
    problems = []
    for cap in caps:
        for ep in cap["endpoints"]:
            if ep not in known:
                problems.append(f"unknown endpoint {ep!r} (capability: {cap['action']})")
        key = cap.get("label")
        if key and key not in i18n:
            problems.append(f"unknown i18n key {key!r} (capability: {cap['action']})")
    return problems


def is_protocol_route(path: str) -> bool:
    """OAuth/MCP plumbing and the SPA shell — driven by clients, not by people."""
    return (
        path == "/"
        or path.startswith("/.well-known/")
        or path.startswith("/mcp")
        or path in {"/authorize", "/consent", "/register", "/token", "/docs/oauth2-redirect"}
    )


def uncovered_routes(caps: list[Cap], routes: list[dict]) -> list[dict]:
    claimed = {ep for cap in caps for ep in cap["endpoints"]}
    return [r for r in routes if f"{r['method']} {r['path']}" not in claimed]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render(
    caps: list[Cap],
    routes: list[dict],
    i18n: dict[str, str],
    statuses: dict[str, list[str]],
    migrations: list[tuple[str, str]],
    mcp_tools: list[str],
    screens: dict[str, str],
    fe_calls: set[str],
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# UI inventory")
    add("")
    add(
        "<!-- GENERATED FILE — do not edit by hand. "
        "Regenerate with `make ui-inventory` (see scripts/gen_ui_inventory.py). -->"
    )
    add("")
    add(
        "Every user-facing capability in VTS: what it acts on, what you can do, "
        "which states it moves through, which endpoint serves it, and where it "
        "lives in the interface. Rows are derived from the FastAPI route table, "
        "`vts/static/index.html`, `vts/static/app.js`, `vts/static/i18n/en.js`, "
        "the `StrEnum`s in `vts/db/models.py`, and the Alembic migrations — "
        "nothing here is written from memory."
    )
    add("")
    add(
        f"**Counts:** {len(caps)} capabilities · {len(routes)} routes "
        f"({sum(1 for r in routes if r['in_schema'])} in the OpenAPI schema, "
        f"{sum(1 for r in routes if not r['in_schema'])} hidden) · "
        f"{len(mcp_tools)} MCP tools · {len(i18n)} English UI strings."
    )
    add("")

    # ---- Capabilities, grouped by entity ----
    add("## Capabilities by entity")
    add("")
    grouped: dict[str, list[Cap]] = defaultdict(list)
    for cap in caps:
        grouped[cap["entity"]].append(cap)

    for entity, items in grouped.items():
        add(f"### {entity}")
        add("")
        add("| Action | Label (en) | States | Endpoint(s) | Screen |")
        add("| --- | --- | --- | --- | --- |")
        for cap in items:
            label = i18n.get(cap["label"] or "", "—") if cap["label"] else "—"
            eps = "<br>".join(f"`{md_escape(e)}`" for e in cap["endpoints"]) or "—"
            add(
                f"| {md_escape(cap['action'])} | {md_escape(label)} | "
                f"{md_escape(cap['states'])} | {eps} | {md_escape(cap['screen'])} |"
            )
        add("")

    # ---- No-UI surface ----
    add("## Reachable without a screen")
    add("")
    add(
        "These capabilities exist in the API but have no control anywhere in the "
        "web UI. They are reached from scripts, the MCP server, or the browser "
        "itself."
    )
    add("")
    no_ui = [c for c in caps if "No UI" in c["screen"] or "No screen" in c["screen"]]
    add("| Entity | Action | Endpoint(s) |")
    add("| --- | --- | --- |")
    for cap in no_ui:
        eps = "<br>".join(f"`{md_escape(e)}`" for e in cap["endpoints"]) or "—"
        add(f"| {md_escape(cap['entity'])} | {md_escape(cap['action'])} | {eps} |")
    add("")

    # ---- States ----
    add("## States")
    add("")
    for name, values in statuses.items():
        add(f"### `{name}`")
        add("")
        if name == "TaskStatus":
            add("| Value | Label (en) |")
            add("| --- | --- |")
            for value in values:
                add(f"| `{value}` | {md_escape(i18n.get(f'status.{value}', '—'))} |")
        else:
            add(", ".join(f"`{v}`" for v in values))
        add("")

    # ---- Pipeline steps ----
    steps = {k: v for k, v in i18n.items() if k.startswith("steps.")}
    if steps:
        add("### Pipeline steps")
        add("")
        add("Step names a task moves through, in the order the UI declares them.")
        add("")
        add("| Step | Label (en) |")
        add("| --- | --- |")
        for key, label in steps.items():
            add(f"| `{key.split('.', 1)[1]}` | {md_escape(label)} |")
        add("")

    # ---- Screens ----
    add("## Screens")
    add("")
    add(
        "The web UI is a single page (`vts/static/index.html`): one New Task card, "
        "one Tasks list, and a set of `<dialog>` overlays opened from the header "
        "menu or a task card."
    )
    add("")
    add("| Dialog id | Title (en) |")
    add("| --- | --- |")
    for sid, title in screens.items():
        add(f"| `#{sid}` | {md_escape(title)} |")
    add("")

    # ---- MCP ----
    add("## MCP tools")
    add("")
    add(
        "`vts/mcp/tools_registry/` exposes the same capabilities to agents. Tools with "
        "no matching UI control are the practical reason the “no screen” list "
        "above is not a gap."
    )
    add("")
    add(", ".join(f"`{t}`" for t in mcp_tools))
    add("")

    # ---- Route coverage ----
    add("## Route coverage")
    add("")
    leftovers = uncovered_routes(caps, routes)
    protocol = [r for r in leftovers if is_protocol_route(r["path"])]
    unexplained = [r for r in leftovers if not is_protocol_route(r["path"])]

    add(
        "Machine-facing routes — the OAuth authorisation-server endpoints that "
        "MCP clients drive, plus the SPA entry point. No user ever navigates to "
        "them directly, so they carry no capability row:"
    )
    add("")
    for route in protocol:
        add(f"- `{route['method']} {route['path']}`")
    add("")
    if unexplained:
        add("Routes not claimed by any capability above (each is a documentation gap):")
        add("")
        for route in unexplained:
            add(f"- `{route['method']} {route['path']}`")
    else:
        add("Every remaining route in the FastAPI app is claimed by a capability above.")
    add("")
    add("Frontend call sites with no matching route (would be a bug):")
    add("")
    patterns = {route_pattern(r["path"]) for r in routes}
    orphans = sorted(
        c
        for c in fe_calls
        if not call_matches(c, patterns)
        and c not in SW_VIRTUAL_PATHS
        and c not in {"/", "/api"}  # "/api/" is a base-path prefix, not a call
    )
    if orphans:
        for call in orphans:
            add(f"- `{call}`")
    else:
        add("- none")
    add("")

    # ---- Schema history ----
    add("## Schema history")
    add("")
    add("Migrations that introduced or changed the entities above.")
    add("")
    add("| Revision | Change |")
    add("| --- | --- |")
    for stem, doc in migrations:
        add(f"| `{stem}` | {md_escape(doc)} |")
    add("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed doc differs from freshly generated output",
    )
    parser.add_argument("--json", action="store_true", help="dump the raw model instead of markdown")
    args = parser.parse_args()

    routes = load_routes()
    i18n = load_i18n()
    statuses = load_statuses()
    migrations = load_migrations()
    mcp_tools = load_mcp_tools()
    screens = load_screens(i18n)
    fe_calls = load_frontend_calls()

    problems = verify(CAPABILITIES, routes, i18n)
    if problems:
        print("ui-inventory: capability table is out of sync with the code:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(
            {
                "capabilities": CAPABILITIES,
                "routes": routes,
                "statuses": statuses,
                "mcp_tools": mcp_tools,
            },
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        return 0

    content = render(
        CAPABILITIES, routes, i18n, statuses, migrations, mcp_tools, screens, fe_calls
    )

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != content:
            print(
                f"ui-inventory: {OUT.relative_to(ROOT)} is stale — run `make ui-inventory`.",
                file=sys.stderr,
            )
            return 1
        print(f"ui-inventory: {OUT.relative_to(ROOT)} is up to date.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"ui-inventory: wrote {OUT.relative_to(ROOT)} ({len(CAPABILITIES)} capabilities).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

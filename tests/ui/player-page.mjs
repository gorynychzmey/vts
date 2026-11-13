// Renders the real /player page for a scenario, through the same Python
// function the route calls (`_player_page_html`). A scenario that assembled the
// page from the template files itself would test its own copy of the assembly,
// and silently go stale whenever the real one changed.
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

// VTS_PYTHON wins; then the checkout's own .venv (a worktree has its own);
// then whatever python3 is on PATH (CI installs the requirements there).
function python() {
  if (process.env.VTS_PYTHON) return process.env.VTS_PYTHON;
  const venv = path.join(REPO, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

// args: the keyword arguments of `_player_page_html`.
export function renderPlayerPage(args) {
  const script = `
import json, sys
from vts.api.routers.artifacts import _player_page_html
sys.stdout.write(_player_page_html(**json.load(sys.stdin)))
`;
  const r = spawnSync(python(), ["-c", script], {
    cwd: REPO, input: JSON.stringify(args), encoding: "utf8",
  });
  if (r.status !== 0) throw new Error(`rendering the player page failed: ${r.stderr}`);
  return r.stdout;
}

// The MCP Apps player view (`ui://vts/player`) as the server serves it.
export function renderMcpPlayerView() {
  const r = spawnSync(python(), ["-c",
    "import sys; from vts.mcp.ui import player_view_html; sys.stdout.write(player_view_html())"], {
    cwd: REPO, encoding: "utf8",
  });
  if (r.status !== 0) throw new Error(`rendering the MCP player view failed: ${r.stderr}`);
  return r.stdout;
}

// The MCP Apps search view (`ui://vts/search`) as the server serves it.
export function renderMcpSearchView() {
  const r = spawnSync(python(), ["-c",
    "import sys; from vts.mcp.ui import search_view_html; sys.stdout.write(search_view_html())"], {
    cwd: REPO, encoding: "utf8",
  });
  if (r.status !== 0) throw new Error(`rendering the MCP search view failed: ${r.stderr}`);
  return r.stdout;
}

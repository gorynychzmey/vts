// A stub server for the live-library scenarios (vts-lzad): it orders and pages
// /api/recordings like the endpoint (newest first by (listed_at, id), cursors,
// `task_id` lookups, the `q` name filter) and holds the SSE stream open so a
// scenario can push task events into it. A stub answering /api/events with
// JSON makes the page reconnect and reload lists on its own, which hides a
// missing live update.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { STATIC_DIR } from "./harness.mjs";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

export function recording(id, taskId, listedAt, title) {
  return {
    id, source_task_id: taskId, title, title_is_custom: false,
    source_url: `file://${title}.m4a`, duration_sec: 630, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [], state: "complete",
    recorded_at: listedAt, listed_at: listedAt,
    created_at: "2026-08-20T09:00:00Z", updated_at: listedAt,
  };
}

// Ascending by (listed_at, id); the server's library order is the reverse.
function cmp(a, b) {
  if (a.listed_at !== b.listed_at) return a.listed_at < b.listed_at ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

// library: the listed recordings (mutable; the scenario adds to it).
// opts.extra: recordings only a task_id lookup finds — listed, but further
//   down than any loaded page.
// opts.holdList(params): awaited before answering a list request (no
//   task_id), so a scenario can keep a page in flight.
export async function startLibraryLiveServer(library, opts = {}) {
  const extra = opts.extra || [];
  const requests = [];
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
  };
  const server = http.createServer(async (req, res) => {
    const [url, qs] = req.url.split("?");
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url === "/api/recordings") {
        const p = new URLSearchParams(qs || "");
        requests.push(p);
        const q = (p.get("q") || "").toLowerCase();
        const match = (r) => !q || (r.title || "").toLowerCase().includes(q);
        const taskIds = p.getAll("task_id");
        let items;
        if (taskIds.length) {
          items = library.concat(extra).filter((r) => taskIds.includes(r.source_task_id) && match(r));
        } else {
          if (opts.holdList) await opts.holdList(p);
          const listed = library.filter(match).sort(cmp);
          if (p.get("after_ts")) {
            const after = { listed_at: p.get("after_ts"), id: p.get("after_id") };
            items = listed.filter((r) => cmp(r, after) > 0);
            if (p.get("order") !== "asc") items.reverse();
          } else if (p.get("before_ts")) {
            const before = { listed_at: p.get("before_ts"), id: p.get("before_id") };
            items = listed.filter((r) => cmp(r, before) < 0).reverse();
          } else {
            items = listed.reverse();
          }
        }
        const limit = Number(p.get("limit") || 50);
        res.end(JSON.stringify({ items: items.slice(0, limit), total: library.filter(match).length }));
        return;
      }
      res.end(JSON.stringify(url === "/api/tasks" ? [] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") body = body.replaceAll("__VTS_VERSION__", "verify");
    res.setHeader("Content-Type", CT[path.extname(fp)] || "text/plain");
    res.end(body);
  });
  await new Promise((r) => server.listen(0, r));
  return {
    server, requests, emit,
    baseUrl: `http://localhost:${server.address().port}`,
    connected: () => Boolean(sseRes),
  };
}

export const lookupsFor = (requests, taskId) =>
  requests.filter((p) => p.getAll("task_id").includes(taskId));

// A request for the list itself: no cursor and no task lookup.
export const fullLoads = (requests) =>
  requests.filter((p) => !p.get("task_id") && !p.get("after_ts") && !p.get("before_ts"));

// Polls a condition on the stub side (requests seen, stream open).
export async function waitUntil(cond, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (cond()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return cond();
}

// Opens the page with error capture and switches to the library.
export async function openLibrary(browser, baseUrl, viewport = { width: 1100, height: 800 }) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
  });
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#main-tab-library", { timeout: 8000 });
  await page.click("#main-tab-library");
  return { page, errors };
}

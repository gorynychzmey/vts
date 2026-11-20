// A recording shown in the library updates its state mark in place when its
// task's status changes: "processing" goes away once the task completes,
// without reloading the list (vts-lzad).
//
// The SSE stream is held open and events are pushed into it; a JSON answer on
// /api/events would make the page reload the list on its own and hide a
// missing in-place update.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { STATIC_DIR, launch } from "../harness.mjs";

export const name = "library-state-updates-live";

const REC = "cccccccc-0000-0000-0000-000000000001";
const TASK = "dddddddd-0000-0000-0000-000000000001";
const OTHER = "cccccccc-0000-0000-0000-000000000002";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

function recording(id, taskId, listedAt, title) {
  return {
    id, source_task_id: taskId, title, title_is_custom: false,
    source_url: `file://${title}.m4a`, duration_sec: 630, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [], state: "complete",
    recorded_at: listedAt, listed_at: listedAt,
    created_at: "2026-08-20T09:00:00Z", updated_at: listedAt,
  };
}

// Newest first by (listed_at, id), as the server orders the library.
function cmp(a, b) {
  if (a.listed_at !== b.listed_at) return a.listed_at < b.listed_at ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

async function startServer(library) {
  const requests = [];
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
  };
  const server = http.createServer((req, res) => {
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
        const listed = library.slice().sort(cmp);
        let items;
        const taskIds = p.getAll("task_id");
        if (taskIds.length) {
          items = listed.filter((r) => taskIds.includes(r.source_task_id));
        } else if (p.get("after_ts")) {
          const after = { listed_at: p.get("after_ts"), id: p.get("after_id") };
          items = listed.filter((r) => cmp(r, after) > 0);
          if (p.get("order") !== "asc") items.reverse();
        } else {
          items = listed.reverse();
        }
        const limit = Number(p.get("limit") || 50);
        res.end(JSON.stringify({ items: items.slice(0, limit), total: library.length }));
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

const lookupsFor = (requests, taskId) =>
  requests.filter((p) => p.getAll("task_id").includes(taskId)).length;
// A request for the list itself: no cursor and no task lookup. One is the
// initial load; a second one would mean the list was reloaded from scratch.
const fullLoads = (requests) =>
  requests.filter((p) => !p.get("task_id") && !p.get("after_ts") && !p.get("before_ts")).length;

// Polls a condition on the stub side (requests seen, stream open).
async function waitUntil(cond, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (cond()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return cond();
}

export async function run() {
  const failures = [];
  // duration_sec is set so the meta line stays (with "10:30 · RU") once the
  // state label goes: the assertion then reads text, not a missing element.
  const processing = { ...recording(REC, TASK, "2026-08-20T11:00:00Z", "live"), state: "processing" };
  const library = [
    processing,
    recording(OTHER, "dddddddd-0000-0000-0000-000000000002", "2026-08-20T10:00:00Z", "other"),
  ];
  const { server, baseUrl, requests, emit, connected } = await startServer(library);
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    const sel = `#library-list [data-recording-id="${REC}"]`;
    await page.waitForSelector(sel, { timeout: 8000 });
    if (!(await waitUntil(connected, 5000))) {
      failures.push("setup: the page never opened the SSE stream");
      return failures;
    }
    const before = await page.evaluate((s) => {
      const el = document.querySelector(s);
      el.dataset.marker = "before";
      return el.querySelector(".library-meta")?.textContent || "";
    }, sel);
    if (!before.includes("processing")) {
      failures.push(`setup: the card does not say "processing" (${JSON.stringify(before)})`);
      return failures;
    }

    // The task finishes; the server now reports the recording complete.
    library[0] = { ...processing, state: "complete" };
    emit("task_status", { task_id: TASK, data: { status: "completed" } });
    const cleared = await page.waitForFunction(
      (s) => {
        const meta = document.querySelector(s)?.querySelector(".library-meta");
        return Boolean(meta) && !meta.textContent.includes("processing");
      },
      sel, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!cleared) {
      const now = await page.evaluate((s) => document.querySelector(s)?.querySelector(".library-meta")?.textContent, sel);
      failures.push(`the card still reads ${JSON.stringify(now)} after its task completed`);
    }
    const state = await page.evaluate((s) => {
      const el = document.querySelector(s);
      return {
        marker: el?.dataset.marker || null,
        metas: el ? el.querySelectorAll(".library-meta").length : 0,
        text: el?.querySelector(".library-meta")?.textContent || "",
      };
    }, sel);
    if (state.marker !== "before") failures.push("the card was rebuilt instead of updated in place");
    if (state.metas !== 1) failures.push(`the card carries ${state.metas} meta lines, expected 1`);
    if (cleared && !state.text.includes("RU")) failures.push(`the meta line lost its facts: ${JSON.stringify(state.text)}`);
    if (lookupsFor(requests, TASK) < 1) failures.push("no lookup of the task's recording was made");
    const loads = fullLoads(requests);
    if (loads !== 1) failures.push(`the library list was fetched from scratch ${loads} times, expected 1`);
    const banner = await page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden === false);
    if (banner) failures.push("a shown recording's update raised the new-recordings banner");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

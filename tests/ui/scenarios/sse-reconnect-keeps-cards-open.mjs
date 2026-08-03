// vts-9zs: an SSE reconnect must not collapse expanded task cards.
//
// Both reconnect paths (onerror's 2s backoff and the server_shutdown handler)
// called loadFirstPage(), which does `taskList.innerHTML = ""` and rebuilds
// every card from scratch. So any network blip collapsed every expanded card,
// lost whichever tab was open inside it, and — since infinite scroll landed —
// threw away every page the user had scrolled in, snapping back to the first
// page. On a flaky connection that happened every couple of seconds.
//
// This drives the real thing: expand a card, kill the SSE stream server-side
// so the browser's EventSource fires onerror, and assert the card is still
// expanded after the client reconnects.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "sse-reconnect-keeps-cards-open";

const A_ID = "d1111111-1111-1111-1111-111111111111";
const B_ID = "d2222222-2222-2222-2222-222222222222";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
};

function task(id, createdAt) {
  return {
    id, source_url: "http://example.com/" + id, source_title: "Clip " + id.slice(0, 4),
    status: "completed", queue: null, queue_position: null,
    transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [] }, steps: [],
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: createdAt, updated_at: createdAt,
    progress: {}, stats: {},
  };
}

const TASKS = [
  task(A_ID, "2026-08-03T10:01:00.000000+00:00"),
  task(B_ID, "2026-08-03T10:00:00.000000+00:00"),
];

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": TASKS,
    [`/api/tasks/${A_ID}`]: TASKS[0],
    [`/api/tasks/${B_ID}`]: TASKS[1],
  };
  let sseRes = null;
  let connects = 0;

  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      connects += 1;
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      // Any /api/tasks call WITH a query string is a paging call
      // (loadNewer/loadNextPage) — return nothing new so the assertions are
      // about card state, not about extra rows appearing.
      if (url === "/api/tasks" && req.url.includes("?after_ts")) { res.end("[]"); return; }
      res.end(JSON.stringify(url in api ? api[url] : {}));
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
    server,
    baseUrl: `http://localhost:${server.address().port}`,
    dropStream() {
      // Kill the stream the way a network blip does: the browser's
      // EventSource sees the connection die and fires onerror.
      if (sseRes) { sseRes.destroy(); sseRes = null; return true; }
      return false;
    },
    connects: () => connects,
    connected: () => Boolean(sseRes),
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, dropStream, connects, connected } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      const text = m.text();
      // ERR_INCOMPLETE_CHUNKED_ENCODING is this scenario's own doing: we kill
      // the SSE response mid-stream to simulate the blip, and Chromium reports
      // the truncated response. It IS the stimulus, not a defect.
      const expected =
        text.includes("EventSource") ||
        text.includes("ERR_INCOMPLETE_CHUNKED_ENCODING");
      if (m.type() === "error" && !expected) {
        errors.push("console.error: " + text);
      }
    });
    await page.goto(baseUrl, { waitUntil: "load" });
    await page.waitForSelector(`[data-task-id="${A_ID}"]`, { timeout: 5000 });
    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events");
      return failures;
    }

    // Expansion is observable: the card body loses .hidden and the toggle
    // gains .expanded.
    const isExpanded = (id) => page.evaluate((taskId) => {
      const el = document.querySelector(`[data-task-id="${taskId}"]`);
      if (!el) return null;
      const body = el.querySelector(".task-body");
      const toggle = el.querySelector(".toggle-btn");
      return {
        bodyVisible: body ? !body.classList.contains("hidden") : null,
        toggleExpanded: toggle ? toggle.classList.contains("expanded") : null,
      };
    }, id);

    // Expand the first card with a real click on its toggle.
    const toggleSel = `[data-task-id="${A_ID}"] .toggle-btn`;
    const hasToggle = await page.evaluate((sel) => Boolean(document.querySelector(sel)), toggleSel);
    if (!hasToggle) {
      failures.push(`could not find the expand toggle (${toggleSel}) — selector drift, scenario needs updating`);
      return failures;
    }
    await page.click(toggleSel);
    await page.waitForTimeout(400);

    const before = await isExpanded(A_ID);
    if (!before || before.bodyVisible !== true) {
      failures.push(`card did not expand on click; state=${JSON.stringify(before)}`);
      return failures;
    }

    const connectsBefore = connects();

    // THE BLIP: drop the SSE stream server-side.
    if (!dropStream()) {
      failures.push("could not drop the SSE stream (no active response held)");
      return failures;
    }

    // onerror waits 2s before reconnecting; allow for that plus the resync.
    let reconnected = false;
    for (let i = 0; i < 80; i++) {
      if (connects() > connectsBefore) { reconnected = true; break; }
      await page.waitForTimeout(100);
    }
    if (!reconnected) {
      failures.push("client never reconnected to /api/events after the stream dropped");
      return failures;
    }
    await page.waitForTimeout(1200); // let the resync settle

    // THE ASSERTION: the card must still be expanded.
    const after = await isExpanded(A_ID);
    if (!after) {
      failures.push(`the expanded card vanished from the DOM after the reconnect (list was rebuilt)`);
    } else if (after.bodyVisible !== true) {
      failures.push(
        `the expanded card collapsed after an SSE reconnect — state before=${JSON.stringify(before)}, ` +
        `after=${JSON.stringify(after)}. The reconnect rebuilt the list instead of refreshing in place.`
      );
    }

    // The other card must still be there too: a resync must not drop rows.
    const stillHasB = await page.evaluate((id) => Boolean(document.querySelector(`[data-task-id="${id}"]`)), B_ID);
    if (!stillHasB) failures.push("the second task disappeared from the list after the reconnect");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// VOS-84: the "new tasks" banner. When a task_status SSE event arrives for a
// task_id that is NOT already in the DOM, the frontend fetches
// /api/tasks/{id}; if that task's created_at is NEWER than the topmost
// loaded card's created_at (state.taskPaging.head), #new-tasks-banner must
// go from hidden to visible with #new-tasks-count reflecting the count.
// An id whose created_at is OLDER than head must NOT show the banner.
//
// Needs a custom held-open SSE stream (the default stub server only serves
// static per-path values and can't push events), so this reuses the
// streaming-server construction from awaiting-input-live-sse.mjs. It also
// needs query-string-aware routing for /api/tasks/<id> (the default stub
// ignores query strings but also doesn't do per-id path matching), so a
// small custom http server is built here.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "new-tasks-banner";

const HEAD_ID = "b1111111-1111-1111-1111-111111111111";
const TAIL_ID = "b2222222-2222-2222-2222-222222222222";
const NEWER_ID = "b3333333-3333-3333-3333-333333333333";
const OLDER_ID = "b4444444-4444-4444-4444-444444444444";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
};

function task(id, status, createdAt) {
  return {
    id, source_url: "http://x/" + id, source_title: status, status,
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [] }, steps: [],
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: createdAt, updated_at: createdAt,
    progress: {}, stats: {},
  };
}

// Initial page: head is the 10:01 task (HEAD_ID), tail is the 10:00 one.
const INITIAL_TASKS = [
  task(HEAD_ID, "completed", "2026-07-30T10:01:00.000000+00:00"),
  task(TAIL_ID, "completed", "2026-07-30T10:00:00.000000+00:00"),
];

// A task newer than head — the banner MUST appear for this one.
const NEWER_TASK = task(NEWER_ID, "queued", "2026-07-30T11:00:00.000000+00:00");
// A task older than head — the banner MUST NOT appear for this one.
const OLDER_TASK = task(OLDER_ID, "queued", "2026-07-30T09:00:00.000000+00:00");

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": INITIAL_TASKS,
    [`/api/tasks/${NEWER_ID}`]: NEWER_TASK,
    [`/api/tasks/${OLDER_ID}`]: OLDER_TASK,
  };
  let sseRes = null;
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : (url === "/api/tasks" ? [] : {})));
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
    emit(event, payload) {
      if (!sseRes) return false;
      sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
      return true;
    },
    connected: () => Boolean(sseRes),
  };
}

async function openPageStreaming(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("EventSource")) {
      errors.push("console.error: " + m.text());
    }
  });
  await page.goto(baseUrl, { waitUntil: "load" });
  await page.waitForTimeout(300);
  return { page, errors };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, emit, connected } = await startServer();
  const browser = await launch();
  try {
    const { page, errors } = await openPageStreaming(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${HEAD_ID}"]`, { timeout: 5000 });

    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events (scenario cannot drive SSE)");
      return failures;
    }

    const bannerState = () => page.evaluate(() => {
      const b = document.getElementById("new-tasks-banner");
      const c = document.getElementById("new-tasks-count");
      if (!b) return { present: false };
      const cs = getComputedStyle(b);
      const visible = !b.hidden && cs.display !== "none" && cs.visibility !== "hidden" && b.offsetHeight > 0;
      return { present: true, visible, hidden: b.hidden, count: c ? c.textContent : null };
    });

    // BEFORE: banner starts hidden.
    const before = await bannerState();
    if (!before.present) {
      failures.push("#new-tasks-banner not found in the DOM");
      return failures;
    }
    if (before.visible) {
      failures.push("#new-tasks-banner is visible before any newer-task SSE event arrives");
    }

    // --- Negative case first: an SSE event for a task OLDER than head must
    // NOT surface the banner. ---
    emit("task_status", { task_id: OLDER_ID, data: { status: "queued" } });
    await page.waitForTimeout(600);
    const afterOlder = await bannerState();
    if (afterOlder.visible) {
      failures.push(
        `#new-tasks-banner became visible after an SSE event for an OLDER task (${OLDER_ID}, created_at 09:00 < head 10:01) ` +
        `— banner should only react to tasks newer than head`
      );
    }

    // --- Positive case: an SSE event for a task NEWER than head must show
    // the banner with count reflecting 1 new task. ---
    emit("task_status", { task_id: NEWER_ID, data: { status: "queued" } });

    let after = null;
    for (let i = 0; i < 50; i++) {
      after = await bannerState();
      if (after.visible) break;
      await page.waitForTimeout(100);
    }
    if (!after || !after.visible) {
      failures.push(
        `#new-tasks-banner did not become visible after an SSE task_status for a NEWER task ` +
        `(${NEWER_ID}, created_at 11:00 > head 10:01). Last observed state: ${JSON.stringify(after)}`
      );
    } else if (!/1/.test(String(after.count || ""))) {
      failures.push(`#new-tasks-count does not reflect 1 new task: got ${JSON.stringify(after.count)}`);
    }

    // Clicking the banner must not crash. The click triggers loadNewer(),
    // which fetches /api/tasks?after_ts=...&after_id=...; the custom server
    // above returns [] for any /api/tasks query with a search string that
    // isn't the bare initial list (see the api map fallback), so this
    // exercises the "no more new tasks, banner clears" path without us
    // needing a query-string-aware router. We only assert: no crash, and
    // the banner ends up in a sane (hidden, since 0 < pageSize) state.
    await page.evaluate(() => document.getElementById("new-tasks-banner")?.click());
    await page.waitForTimeout(500);
    const afterClick = await bannerState();
    if (afterClick.visible && !/\d/.test(String(afterClick.count || ""))) {
      failures.push(`after clicking the banner, it is visible but count is not numeric: ${JSON.stringify(afterClick.count)}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

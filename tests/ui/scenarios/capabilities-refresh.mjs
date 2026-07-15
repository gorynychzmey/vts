// Regression (vts-c2n): the "restart summary" buttons are gated on
// runtime.capabilities, which is server-computed and delivered on TaskOut.
// createRuntime() set it once at load; the SSE patch functions mutate the
// runtime in place and NOTHING re-polled it (loadTasks() only runs on user
// action or SSE reconnect). So a user watching a task finish LIVE saw the
// restart buttons stay greyed out forever — recoverable only by a reload.
//
// This drives the REAL path: a real EventSource against a real SSE stream,
// emitting a real `task_status` completed event. patchTaskStatus refetches
// GET /api/tasks/<id> (it always did) and must now copy `capabilities` over.
//
// The stub server here is bespoke (not startStubServer) because we need a
// streaming /api/events response and a per-id /api/tasks/<id> route that
// answers with the post-completion TaskOut.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

// Same error capture as harness.openPage, but waits for "load" instead of
// "networkidle": our /api/events stream is deliberately held open, so the
// network is NEVER idle and openPage() would time out.
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

export const name = "capabilities-refresh";

const TASK_ID = "77777777-7777-7777-7777-777777777777";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

// Mirrors vts.services.task_status.status_flags() (same source of truth as
// scenarios/status-predicates.mjs).
const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  running:   { is_active:true,  is_pending:false, is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  waiting:   { is_active:true,  is_pending:true,  is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  paused:    { is_active:false, is_pending:false, is_finished:false, shows_progress:false, can_pause:false, can_resume:true,  can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
  failed:    { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:true,  can_archive:true  },
  archived:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
  canceled:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
};

// The task as it is DURING the run: not finished, so the server says the
// restart actions are unavailable.
const RUNNING_TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Live task",
  status: "running", queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: {
    transcript: true,
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [],
  },
  steps: [{ name: "summarize_windows", status: "running", started_at: "2026-07-14T10:00:00Z", finished_at: null }],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:00:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 1, total: 2 } },
  stats: {},
};

// The SAME task after the worker finished it — this is exactly what the real
// GET /api/tasks/<id> returns once the run completes (capabilities flip true).
const COMPLETED_TASK = {
  ...RUNNING_TASK,
  status: "completed",
  summary_path: "/x/summary/final.md",
  options: {
    ...RUNNING_TASK.options,
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "summarize_windows", status: "completed", started_at: "2026-07-14T10:00:00Z", finished_at: "2026-07-14T10:01:00Z" },
    { name: "summarize_final", status: "completed", started_at: "2026-07-14T10:01:00Z", finished_at: "2026-07-14T10:02:00Z" },
  ],
  capabilities: { can_restart_summary: true, can_restart_final_summary: true },
  updated_at: "2026-07-14T10:02:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 2, total: 2 } },
};

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [RUNNING_TASK],
  };
  let sseRes = null;
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];

    // Real SSE stream: held open so the page keeps a live EventSource.
    if (url === "/api/events") {
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }

    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      // The refetch endpoint patchTaskStatus hits: answers with the task as it
      // now is on the server — completed, capabilities true.
      if (url === `/api/tasks/${TASK_ID}`) { res.end(JSON.stringify(COMPLETED_TASK)); return; }
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
    // Push a real SSE event to the live browser EventSource.
    emit(event, payload) {
      if (!sseRes) return false;
      sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
      return true;
    },
    connected: () => Boolean(sseRes),
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, emit, connected } = await startServer();
  const browser = await launch();
  try {
    const { page, errors } = await openPageStreaming(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    const restartBtn = () => page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .restart-summary-btn`);
      return el ? { present: true, disabled: el.disabled === true, hidden: el.classList.contains("hidden") } : { present: false };
    }, TASK_ID);
    const finalBtn = () => page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .restart-summary-final-btn`);
      return el ? { present: true, disabled: el.disabled === true, hidden: el.classList.contains("hidden") } : { present: false };
    }, TASK_ID);
    const actionable = (b) => b.present && !b.disabled && !b.hidden;

    // The EventSource must really be connected, otherwise this scenario would
    // be asserting nothing at all.
    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events (scenario cannot drive SSE)");
      return failures;
    }

    // BEFORE: task is running, server says restart not available.
    const before = await restartBtn();
    if (!before.present) {
      failures.push("no .restart-summary-btn on the running task row");
      return failures;
    }
    if (actionable(before)) {
      failures.push("restart-summary-btn is actionable while the task is still running");
    }

    // The task finishes LIVE — a real `task_status` SSE event, exactly what the
    // backend emits. No page reload, no user action, no loadTasks().
    emit("task_status", { task_id: TASK_ID, data: { status: "completed" } });

    // patchTaskStatus refetches GET /api/tasks/<id> then re-renders; wait for
    // the button to settle rather than racing the async refetch.
    await page.waitForTimeout(1000);

    // AFTER: the buttons must be live immediately — this is the regression.
    const after = await restartBtn();
    if (!actionable(after)) {
      failures.push(
        `restart-summary-btn still not actionable after live SSE completion ` +
        `(${JSON.stringify(after)}) — capabilities not refreshed from the refetch; ` +
        `user must reload the page to restart the summary`
      );
    }
    const afterFinal = await finalBtn();
    if (!actionable(afterFinal)) {
      failures.push(
        `restart-summary-final-btn still not actionable after live SSE completion ` +
        `(${JSON.stringify(afterFinal)}) — capabilities not refreshed from the refetch`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

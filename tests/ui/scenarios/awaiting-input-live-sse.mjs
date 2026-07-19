// Regression (vts-552): three defects that all surface when a task enters
// awaiting_input via a LIVE SSE event (the real path), not on page load:
//
//  1. Resolve button hidden. The backend emits
//     task_status { status: "awaiting_input", awaiting_step: "match_speakers" },
//     but the frontend task_status handler read only status/error/failure_code/
//     queue and DROPPED awaiting_step. runtime.awaitingStep stayed "" from the
//     last full load, so the "Доработать" button's gate
//     (awaitingStep === "match_speakers") was false. A page reload fixed it
//     because the full /api/tasks load DOES carry awaiting_step — which is
//     exactly why voice-resolution-dialog.mjs (load-based) never caught this.
//
//  2. Raw status text. No locale defines status.awaiting_input, so statusText()
//     fell back to the raw "awaiting_input" string in every language.
//
//  3. Bogus progress runner. awaiting_input is not an active status
//     (shows_progress=false), but the frontend never consulted that flag and
//     had no awaiting_input branch in computeLocalStepProgress, so it rendered
//     an indeterminate "working" runner as if the task were still processing.
//
// Driven through a real EventSource against a real held-open SSE stream, the
// same construction as capabilities-refresh.mjs.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

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

export const name = "awaiting-input-live-sse";

const TASK_ID = "77777777-7777-7777-7777-777777777777";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

// Mirrors vts.services.task_status.status_flags(): awaiting_input is NOT active
// and shows_progress is false — the frontend must honour that.
const FLAGS = {
  running: {
    is_active: true, is_pending: false, is_finished: false, shows_progress: true,
    can_pause: true, can_resume: false, can_archive: false, needs_input: false,
  },
  awaiting_input: {
    is_active: false, is_pending: false, is_finished: false, shows_progress: false,
    can_pause: false, can_resume: true, can_archive: true, needs_input: true,
  },
};

// Starts life running with diarize enabled, no awaiting_step yet — exactly the
// state a task is in the instant before match_speakers pauses it.
const RUNNING_TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Meeting recording",
  status: "running", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-19T10:00:00Z", finished_at: "2026-07-19T10:01:00Z" },
    { name: "match_speakers", status: "running", started_at: "2026-07-19T10:01:00Z", finished_at: null },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-19T10:00:00Z", updated_at: "2026-07-19T10:01:00Z",
  progress: {}, stats: {},
};

async function startServer() {
  const api = { ...DEFAULT_API, "/api/status-config": { status_flags: FLAGS }, "/api/tasks": [RUNNING_TASK] };
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

    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events (scenario cannot drive SSE)");
      return failures;
    }

    const resolveBtn = () => page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .resolve-voices-btn`);
      return el ? { present: true, hidden: el.classList.contains("hidden"), disabled: el.disabled === true } : { present: false };
    }, TASK_ID);

    // BEFORE: still running — the resolve button must not be shown.
    const before = await resolveBtn();
    if (!before.present) {
      failures.push("no .resolve-voices-btn in the task template");
      return failures;
    }
    if (!before.hidden) {
      failures.push("resolve-voices-btn is visible while the task is still running");
    }

    // The task pauses LIVE: the exact event the backend emits at
    // processor.py — status + awaiting_step together. No reload, no user action.
    emit("task_status", { task_id: TASK_ID, data: { status: "awaiting_input", awaiting_step: "match_speakers" } });
    await page.waitForTimeout(600);

    // 1. Resolve button must appear from the live event alone.
    const after = await resolveBtn();
    if (after.hidden || after.disabled) {
      failures.push(
        `resolve-voices-btn still not actionable after live awaiting_input SSE ` +
        `(${JSON.stringify(after)}) — awaiting_step dropped from the event; user must reload to see the button`
      );
    }

    // 2. Status label must be localized, not the raw "awaiting_input" token.
    const statusText = await page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .task-status`);
      return el ? (el.textContent || "").trim() : null;
    }, TASK_ID);
    if (statusText === null) {
      failures.push("no .task-status element found");
    } else if (/awaiting_input/.test(statusText)) {
      failures.push(`status label shows raw token "${statusText}" — status.awaiting_input i18n key missing`);
    } else if (!statusText) {
      failures.push("status label is empty for awaiting_input");
    }

    // 3. No indeterminate "working" progress runner: awaiting_input is not
    // active, so the local step progress must not animate as if processing.
    const prog = await page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .local-progress`);
      if (!el) return null;
      return { indeterminate: el.classList.contains("indeterminate") };
    }, TASK_ID);
    if (prog && prog.indeterminate) {
      failures.push("local progress shows an indeterminate 'working' runner while awaiting_input (should be idle)");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

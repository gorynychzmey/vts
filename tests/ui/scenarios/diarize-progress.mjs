// vts-06u: diarization now emits `diarize_progress` over SSE; the UI must
// render it. Only the `embeddings` phase carries a total (~98% of the step's
// wall time); the brief segmentation/counting phases carry none and must read
// as running rather than snapping the bar to 0%.
//
// Drives the REAL path: a real EventSource, a real `diarize_progress` event,
// and asserts the observable progress bar — not the internal runtime object.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, launch } from "../harness.mjs";

export const name = "diarize-progress";

const TASK_ID = "88888888-8888-8888-8888-888888888888";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  running: { is_active:true, is_pending:false, is_finished:false, shows_progress:true, can_pause:true, can_resume:false, can_archive:false },
};

// A task stopped in the diarize step: diarize is running, so the bar reflects
// whatever diarize_progress we push.
const RUNNING_TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Diarizing task",
  status: "running", queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "transcribe_segments", status: "completed", started_at: "2026-07-14T10:00:00Z", finished_at: "2026-07-14T10:01:00Z" },
    { name: "diarize", status: "running", started_at: "2026-07-14T10:01:00Z", finished_at: null },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:01:00Z",
  progress: {},
  stats: {},
};

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

async function startServer() {
  const api = {
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [RUNNING_TASK],
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
  };
}

// The overall progress bar's width, as a fraction the harness can compare.
async function barFraction(page) {
  return page.evaluate((id) => {
    const el = document.querySelector(`[data-task-id="${id}"]`);
    if (!el) return null;
    const fill = el.querySelector(".progress-fill, .overall-progress-fill, [class*='progress'][class*='fill']");
    if (!fill) return null;
    const w = fill.style.width || "";
    const m = w.match(/([\d.]+)%/);
    return m ? Number(m[1]) / 100 : null;
  }, TASK_ID);
}

export async function run() {
  const failures = [];
  const { server, baseUrl, emit } = await startServer();
  const browser = await launch();
  try {
    const { page, errors } = await openPageStreaming(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    // The step label must read the localized diarization name, not raw "diarize".
    const stepLabel = await page.evaluate((id) => {
      const el = document.querySelector(`[data-task-id="${id}"]`);
      const lbl = el && el.querySelector("[class*='step-label'], [class*='stepLabel']");
      return lbl ? lbl.textContent : "";
    }, TASK_ID);
    if (/\bdiarize\b/.test(stepLabel)) {
      failures.push(`step label shows raw "diarize" instead of a localized name: ${JSON.stringify(stepLabel)}`);
    }

    // embeddings at 5/10 -> the bar shows ~50% of the diarize step.
    if (!emit("diarize_progress", { task_id: TASK_ID, data: { step: "embeddings", completed: 5, total: 10 } })) {
      failures.push("SSE not connected; diarize_progress not delivered");
    }
    await page.waitForTimeout(250);
    const half = await barFraction(page);
    if (half === null) {
      failures.push("no progress bar fill found after embeddings progress");
    } else if (half <= 0 || half >= 1) {
      failures.push(`embeddings 5/10 should give a partial bar, got fraction ${half}`);
    }

    // Advancing to 9/10 must move the bar forward, not backward.
    emit("diarize_progress", { task_id: TASK_ID, data: { step: "embeddings", completed: 9, total: 10 } });
    await page.waitForTimeout(250);
    const more = await barFraction(page);
    if (more !== null && half !== null && more <= half) {
      failures.push(`progress did not advance: ${half} -> ${more}`);
    }

    // A phase without a total (segmentation) must NOT snap the bar backward.
    // The overall bar is weight-based, so a totalless local step contributes
    // its floor rather than 0 — the point is that it never regresses below the
    // progress already shown, and never crashes.
    const before = await barFraction(page);
    emit("diarize_progress", { task_id: TASK_ID, data: { step: "segmentation", completed: 0, total: 0 } });
    await page.waitForTimeout(250);
    const after = await barFraction(page);
    if (after !== null && before !== null && after < before - 0.01) {
      failures.push(`totalless phase regressed the bar: ${before} -> ${after}`);
    }

    if (errors.length) failures.push(`console errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

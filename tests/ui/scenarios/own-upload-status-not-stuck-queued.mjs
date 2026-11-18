// A freshly uploaded task must not stay "queued" on its card while it runs.
//
// The server publishes task_status BEFORE answering the create/finalize
// request, and the worker can claim the task and publish "running" in that
// same window. Those events arrive while the card is not in the DOM yet, so
// patchTaskStatus()/patchTaskStep() drop them; the card is then rendered from
// the response, which is a snapshot taken at creation time ("queued", no
// steps). Nothing ever re-sends "running", so the card said "queue #1" for the
// whole run while its log showed transcription going on (prod, 2026-11-18).
//
// The stub reproduces that order: inside /finalize it emits queued, running
// and a step event for the task, then answers with the queued snapshot. The
// task's own GET returns the real state. The card must end up showing the
// running status and the step the server reports.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "own-upload-status-not-stuck-queued";

const HEAD_ID = "d1111111-1111-1111-1111-111111111111";
const OWN_ID = "d2222222-2222-2222-2222-222222222222";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  running:   { is_active:true,  is_pending:false, is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
};

function task(id, status, createdAt, extra = {}) {
  return {
    id, source_url: "http://x/" + id, source_title: null, status,
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [] }, steps: [],
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: createdAt, updated_at: createdAt,
    progress: {}, stats: {},
    ...extra,
  };
}

const CREATED_AT = "2026-11-18T12:33:39.000000+00:00";
const INITIAL_TASKS = [task(HEAD_ID, "completed", "2026-11-17T10:00:00.000000+00:00")];
// What the finalize response carries: the snapshot from creation time.
const OWN_SNAPSHOT = task(OWN_ID, "queued", CREATED_AT, { source_url: "file://clip.mkv", queue_position: 1 });
// What the server knows by the time the card exists.
const OWN_NOW = task(OWN_ID, "running", CREATED_AT, {
  source_url: "file://clip.mkv",
  steps: [
    { name: "extract_audio", status: "completed", started_at: "2026-11-18T12:33:40.000000+00:00", finished_at: "2026-11-18T12:33:41.000000+00:00" },
    { name: "transcribe", status: "running", started_at: "2026-11-18T12:33:42.000000+00:00", finished_at: null },
  ],
});

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": INITIAL_TASKS,
    "/api/uploads/config": { chunked_threshold_bytes: 1, chunk_bytes: 8388608, max_upload_bytes: 2147483648 },
    [`/api/tasks/${OWN_ID}`]: OWN_NOW,
    "/api/tasks/queue-positions": {},
  };
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
  };

  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    if (url === "/api/uploads/init" && req.method === "POST") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ upload_id: OWN_ID, chunk_size: 8388608 }));
      return;
    }
    if (url === `/api/uploads/${OWN_ID}` && req.method === "PATCH") {
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ received: size, total_size: size }));
      });
      return;
    }
    if (url === `/api/uploads/${OWN_ID}/finalize` && req.method === "POST") {
      // The worker wins the race: the whole queued -> running transition is
      // published before the response that creates the card.
      emit("task_status", { task_id: OWN_ID, data: { status: "queued" } });
      emit("task_status", { task_id: OWN_ID, data: { status: "running" } });
      emit("step", { task_id: OWN_ID, data: { name: "extract_audio", status: "running" } });
      setTimeout(() => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(OWN_SNAPSHOT));
      }, 500);
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
  return { server, baseUrl: `http://localhost:${server.address().port}`, connected: () => Boolean(sseRes) };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, connected } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
    });
    await page.goto(baseUrl, { waitUntil: "load" });
    await page.waitForSelector(`[data-task-id="${HEAD_ID}"]`, { timeout: 5000 });
    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events");
      return failures;
    }

    await page.evaluate(() => {
      const radio = document.getElementById("source-type-file");
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.waitForTimeout(200);
    await page.setInputFiles("#file-input", { name: "clip.mkv", mimeType: "video/x-matroska", buffer: Buffer.from("0123456789") });
    await page.click("#submit-btn", { force: true });

    await page.waitForSelector(`[data-task-id="${OWN_ID}"]`, { timeout: 5000 }).catch(() => {});

    // Wait on the condition, bounded; a card that stays queued fails below
    // with what it actually shows.
    const readCard = () => page.evaluate((id) => {
      const el = document.querySelector(`[data-task-id="${id}"]`);
      if (!el) return null;
      const st = el.querySelector(".task-status");
      const rt = el._runtime || {};
      return {
        statusClass: st ? st.className : null,
        statusText: st ? st.textContent : null,
        baseStatus: rt.baseStatus,
        transcribeStep: (rt.stepStatusByName || {}).transcribe || null,
        extractStep: (rt.stepStatusByName || {}).extract_audio || null,
      };
    }, OWN_ID);
    let card = null;
    for (let i = 0; i < 30; i++) {
      card = await readCard();
      if (card && card.baseStatus === "running" && card.transcribeStep) break;
      await page.waitForTimeout(100);
    }

    if (!card) {
      failures.push("the uploaded task's card never appeared");
    } else {
      if (card.baseStatus !== "running" || !String(card.statusClass).includes("status-running")) {
        failures.push(
          `the card of a running upload still shows ${JSON.stringify(card.statusText)} ` +
          `(class=${JSON.stringify(card.statusClass)}, baseStatus=${card.baseStatus}) — ` +
          `the running event that beat the create response was lost`
        );
      }
      if (card.transcribeStep !== "running" || card.extractStep !== "completed") {
        failures.push(
          `the card misses the steps the server reports: extract_audio=${card.extractStep}, ` +
          `transcribe=${card.transcribeStep} (expected completed / running)`
        );
      }
    }
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Bug #3 (vts-552): saving the voice-resolution dialog must NOT collapse the
// task card. The old code called loadTasks() after save, which does
// `taskList.innerHTML = ""` and rebuilds every card collapsed, destroying the
// open transcript tab. The fix refreshes the one task IN PLACE.
//
// Bespoke server (like capabilities-refresh.mjs): a held-open /api/events so the
// page keeps a live EventSource and its onerror reconnect never fires
// `void loadTasks()` at +2000ms — that reconnect would ALSO rebuild the list and
// collapse the card, masking whether the SAVE path itself is clean. Holding SSE
// open isolates the save behaviour this scenario is about.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

async function openPageStreaming(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
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

export const name = "resolve-save-keeps-card-open";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const ID = "33333333-3333-3333-3333-333333333333";

const FLAGS = {
  completed: {
    is_active: false, is_pending: false, is_finished: true, shows_progress: false,
    can_pause: false, can_resume: false, can_archive: true, needs_input: false,
  },
};

const DONE_TASK = {
  id: ID, source_url: "http://x/done", source_title: "Finished meeting",
  status: "completed", awaiting_step: "",
  queue: null, queue_position: null,
  transcript_path: "/t", summary_path: "/s",
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-17T10:00:00Z", finished_at: "2026-07-17T10:01:00Z" },
    { name: "match_speakers", status: "completed", started_at: "2026-07-17T10:01:00Z", finished_at: "2026-07-17T10:02:00Z" },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false, can_resolve_speakers: true },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:02:00Z",
  progress: {}, stats: { media_seconds: 300 },
};

const SPEAKER_MATCHES = {
  SPEAKER_00: {
    outcome: "auto", speaker_id: "sp-a", distance: 0.1, share: 0.6, seconds: 180,
    noise: false, display_label: "Голос 1",
    decided_speaker_id: null, decided_name: null, decided_is_noise: null,
    candidates: [{ speaker_id: "sp-a", name: "Anna", distance: 0.1 }],
  },
};

const ALL_SPEAKERS = [{ id: "sp-a", name: "Anna", sample_count: 2 }];

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [DONE_TASK],
    [`/api/tasks/${ID}`]: DONE_TASK,
    [`/api/tasks/${ID}/speaker-matches`]: SPEAKER_MATCHES,
    "/api/speakers": ALL_SPEAKERS,
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
      if (url === `/api/tasks/${ID}/transcript`) {
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        res.end("Голос 1: привет"); return;
      }
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
  return { server, baseUrl: `http://localhost:${server.address().port}`, connected: () => Boolean(sseRes) };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, connected } = await startServer();
  const browser = await launch();
  try {
    const { page, errors } = await openPageStreaming(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${ID}"]`, { timeout: 5000 });
    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource — reconnect loadTasks could mask the bug");
      return failures;
    }

    const cardSel = `[data-task-id="${ID}"]`;
    const bodySel = `${cardSel} .task-body`;

    // Expand the card.
    await page.click(`${cardSel} .task-right-top`);
    await page.waitForTimeout(200);
    if (await page.$eval(bodySel, (el) => el.classList.contains("hidden"))) {
      failures.push("card body did not expand after clicking the toggle");
      return failures;
    }

    // Activate the transcript tab.
    await page.click(`${cardSel} .tab-btn[data-tab="transcript"]`);
    await page.waitForTimeout(200);
    if (!(await page.$eval(`${cardSel} .tab-btn[data-tab="transcript"]`, (el) => el.classList.contains("active")))) {
      failures.push("transcript tab did not activate");
      return failures;
    }

    // Open the resolve dialog and Save.
    await page.click(`${cardSel} .resolve-voices-btn`);
    await page.waitForTimeout(300);
    const dlgOpen = await page.$eval("#voice-resolution-dialog", (d) => d.open === true).catch(() => false);
    if (!dlgOpen) {
      failures.push("voice dialog did not open");
      return failures;
    }
    await page.click("#voice-save");
    // Settle past the in-place refresh + transcript re-fetch. With SSE held
    // open, no reconnect loadTasks fires, so any collapse here is the save path.
    await page.waitForTimeout(700);

    // Bug #3: the card must STILL be expanded on the transcript tab.
    if (await page.$eval(bodySel, (el) => el.classList.contains("hidden"))) {
      failures.push("card collapsed after saving the voice dialog (bug #3) — should stay expanded");
    }
    const transcriptActiveAfter = await page.$eval(
      `${cardSel} .tab-btn[data-tab="transcript"]`, (el) => el.classList.contains("active")
    ).catch(() => false);
    if (!transcriptActiveAfter) {
      failures.push("transcript tab is no longer active after save (card was rebuilt)");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

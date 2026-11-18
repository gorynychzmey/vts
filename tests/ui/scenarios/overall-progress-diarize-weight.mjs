// The overall bar must not read 100% while diarization is still running.
//
// Progress weights come from median step durations, and `diarize` never got
// one: it completes in 0 s on every task that does not ask for it, so its
// median is 0 and the client fell back to a 1-second placeholder against
// ~240 s of transcription. A diarized task therefore read 100% the moment
// transcription ended, while diarization — about as long again — had barely
// started (reported 2026-11-18, "step 7 of 8" at 100%).
//
// A task that does NOT diarize keeps the old behaviour: its diarize step is
// empty, so finishing transcription really is nearly the whole job.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, launch } from "../harness.mjs";

export const name = "overall-progress-diarize-weight";

const DIARIZED = "99999999-0000-0000-0000-000000000001";
const PLAIN = "99999999-0000-0000-0000-000000000002";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  running: { is_active:true, is_pending:false, is_finished:false, shows_progress:true, can_pause:true, can_resume:false, can_archive:false },
};

const HEAD = ["download", "extract_audio", "trim_initial_silence", "segment_audio", "detect_language", "transcribe_segments"];

function task(id, diarize, tail) {
  const steps = HEAD.map((name) => ({ name, status: "completed", started_at: "2026-11-18T10:00:00Z", finished_at: "2026-11-18T10:04:00Z" }));
  return {
    id, source_url: "http://x/" + id, source_title: diarize ? "Diarized" : "Plain",
    status: "running", queue: null, queue_position: null,
    transcript_path: null, summary_path: null,
    options: { transcript: true, diarize, prompts: [] },
    steps: steps.concat(tail),
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:04:00Z",
    progress: {}, stats: {},
  };
}

const TASKS = [
  // Transcription done, diarization just started.
  task(DIARIZED, true, [{ name: "diarize", status: "running", started_at: "2026-11-18T10:04:00Z", finished_at: null }]),
  // Transcription done, the empty diarize step done, merge running.
  task(PLAIN, false, [
    { name: "diarize", status: "completed", started_at: "2026-11-18T10:04:00Z", finished_at: "2026-11-18T10:04:00Z" },
    { name: "merge_transcript", status: "running", started_at: "2026-11-18T10:04:00Z", finished_at: null },
  ]),
];

async function startServer() {
  const api = {
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": TASKS,
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

async function overallPercent(page, id) {
  return page.evaluate((taskId) => {
    const el = document.querySelector(`[data-task-id="${taskId}"] .overall-progress .step-progress-text`);
    const m = el && el.textContent.match(/(\d+)%/);
    return m ? Number(m[1]) : null;
  }, id);
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    await page.goto(baseUrl, { waitUntil: "load" });
    for (const id of [DIARIZED, PLAIN]) {
      await page.waitForSelector(`[data-task-id="${id}"]`, { timeout: 5000 });
    }
    await page.waitForFunction(
      (ids) => ids.every((id) => /\d+%/.test(document.querySelector(`[data-task-id="${id}"] .overall-progress .step-progress-text`)?.textContent || "")),
      [DIARIZED, PLAIN],
      { timeout: 5000 },
    ).catch(() => {});

    const diarized = await overallPercent(page, DIARIZED);
    const plain = await overallPercent(page, PLAIN);
    if (diarized === null) {
      failures.push("no overall percentage on the diarized task");
    } else if (diarized < 35 || diarized > 65) {
      failures.push(
        `diarized task with transcription done and diarization just started shows ${diarized}% overall; ` +
        `diarization takes about as long as transcription, so it should read about half`
      );
    }
    if (plain === null) {
      failures.push("no overall percentage on the plain task");
    } else if (plain < 95) {
      failures.push(`a task that does not diarize, past transcription, shows only ${plain}% — its diarize step is empty`);
    }
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

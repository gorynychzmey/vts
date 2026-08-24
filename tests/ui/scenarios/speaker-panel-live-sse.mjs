// Two regressions in the in-card speakers panel (vts-…, vts-k5va), both only
// visible on the LIVE path — the card is already expanded while diarization is
// still running, and the task pauses underneath it.
//
//  1. Panel never appears. loadSpeakerPanel() is called ONLY from the card's
//     expand handler (app.js). Expanding BEFORE diarization finished loads an
//     empty /speaker-matches, leaves _speakerRows = [] and the panel hidden.
//     The later awaiting_input SSE event repaints the status but never reloads
//     the panel, so it stayed hidden until a full page reload — which is why
//     the load-based scenarios never caught it.
//
//  2. Bindings created no voice fragment. bindSpeakerRow() hard-coded
//     add_fragment:false in every branch, so the server (which only calls
//     add_voice_sample under `if res.add_fragment`) created the person and
//     recorded the decision but never stored a fragment: "персоны есть, а
//     фрагментов у них нет". A manual binding must now send add_fragment:true;
//     confirming an auto-match must NOT (the person was recognised by an
//     existing fragment, and skipping it is what keeps the registry from
//     growing without bound).
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "speaker-panel-live-sse";

const TASK_ID = "88888888-8888-8888-8888-888888888888";
const ANNA_ID = "11111111-2222-3333-4444-555555555555";
const BORIS_ID = "66666666-7777-8888-9999-000000000000";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

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

const RUNNING_TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Meeting recording",
  status: "running", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "running", started_at: "2026-07-19T10:00:55Z", finished_at: null },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-19T10:00:00Z", updated_at: "2026-07-19T10:01:00Z",
  progress: {}, stats: {},
};

const SPEAKERS = [
  { id: ANNA_ID, name: "Anna", sample_count: 2 },
  { id: BORIS_ID, name: "Boris", sample_count: 1 },
];

// SPEAKER_00 is a confident auto-match on Anna; SPEAKER_01 is a "grey" near-miss
// the operator has to resolve by hand — the two branches that must disagree
// about add_fragment.
const MATCHES_AFTER = {
  SPEAKER_00: {
    // speaker_id at the top level is what the matcher writes for an `auto`
    // outcome (speaker_match.py) — the auto-bound person. Without it the row
    // does not count as auto-matched and the accept_auto branch never runs.
    outcome: "auto", share: 0.6, seconds: 600, speaker_id: ANNA_ID,
    decided_speaker_id: null, decided_is_noise: null,
    candidates: [{ speaker_id: ANNA_ID, name: "Anna", distance: 0.12 }],
  },
  SPEAKER_01: {
    outcome: "grey", share: 0.4, seconds: 400,
    decided_speaker_id: null, decided_is_noise: null,
    candidates: [{ speaker_id: BORIS_ID, name: "Boris", distance: 0.42 }],
  },
};

async function startServer() {
  // Diarization has NOT produced speakers yet: this is what the card sees when
  // it is expanded early.
  let matches = {};
  const posts = [];
  let sseRes = null;

  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [RUNNING_TASK],
    "/api/speakers": SPEAKERS,
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
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") {
        let body = "";
        req.on("data", (c) => { body += c; });
        req.on("end", () => {
          if (url === `/api/tasks/${TASK_ID}/speakers`) {
            try { posts.push(JSON.parse(body)); } catch { posts.push({ parseError: body }); }
          }
          res.end(JSON.stringify({ results: {} }));
        });
        return;
      }
      if (url === `/api/tasks/${TASK_ID}/speaker-matches`) {
        res.end(JSON.stringify(matches));
        return;
      }
      if (url === `/api/tasks/${TASK_ID}`) {
        res.end(JSON.stringify({ ...RUNNING_TASK, status: "awaiting_input", awaiting_step: "match_speakers" }));
        return;
      }
      res.end(JSON.stringify(url in api ? api[url] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") {
      res.setHeader("Content-Type", CT[ext] || "application/octet-stream");
      res.end(fs.readFileSync(fp));
      return;
    }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") body = body.replaceAll("__VTS_VERSION__", "verify");
    res.setHeader("Content-Type", CT[ext] || "text/plain");
    res.end(body);
  });

  await new Promise((r) => server.listen(0, r));
  return {
    server,
    baseUrl: `http://localhost:${server.address().port}`,
    posts,
    finishDiarization() { matches = MATCHES_AFTER; },
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
  const srv = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(srv.baseUrl, { waitUntil: "load" });
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    for (let i = 0; i < 50 && !srv.connected(); i++) await page.waitForTimeout(100);
    if (!srv.connected()) {
      failures.push("browser never opened an EventSource (scenario cannot drive SSE)");
      return failures;
    }

    // The user expands the card WHILE diarization is still running — there are
    // no speakers yet, so the panel legitimately stays hidden.
    await page.click(`[data-task-id="${TASK_ID}"] .task-right-top`);
    await page.waitForTimeout(400);

    const panelVisible = () => page.evaluate((i) => {
      const box = document.querySelector(`[data-task-id="${i}"] .speaker-box`);
      if (!box) return { present: false };
      return {
        present: true,
        hidden: box.classList.contains("hidden"),
        rows: box.querySelectorAll(".speaker-box-list .voice-row").length,
      };
    }, TASK_ID);

    const early = await panelVisible();
    if (!early.present) {
      failures.push("no .speaker-box in the task card template");
      return failures;
    }
    if (!early.hidden) {
      failures.push("speakers panel is visible while diarization has produced no speakers yet");
    }

    // Diarization finishes and the task pauses — the real live path.
    srv.finishDiarization();
    srv.emit("task_status", {
      task_id: TASK_ID,
      data: { status: "awaiting_input", awaiting_step: "match_speakers" },
    });

    // Poll rather than a fixed wait: the panel load is an async fetch chain.
    let after = null;
    for (let i = 0; i < 40; i++) {
      await page.waitForTimeout(100);
      after = await panelVisible();
      if (!after.hidden && after.rows > 0) break;
    }
    if (after.hidden || !after.rows) {
      failures.push(
        `speakers panel did not appear after a live awaiting_input SSE event ` +
        `(${JSON.stringify(after)}) — the user has to reload the page to bind voices`
      );
      return failures;
    }

    // --- Binding must carry add_fragment (vts-k5va) -------------------------
    // Drive a real hand-binding through the UI: the grey row shows a similarity
    // chip ("Похоже на: Boris"), and clicking it binds straight away — the same
    // path the user took. No test-only hook: the point is what the panel's own
    // code puts on the wire.
    const chip = await page.$(`[data-task-id="${TASK_ID}"] .speaker-box .speaker-chip`);
    if (!chip) {
      failures.push(
        "no .speaker-chip to bind with — the grey row rendered no similarity chip, " +
        "so the manual-binding path cannot be exercised"
      );
      return failures;
    }
    await chip.click();
    await page.waitForTimeout(600);

    const post = srv.posts[srv.posts.length - 1];
    if (!post || !Array.isArray(post.resolutions)) {
      failures.push("panel binding sent no /speakers payload with resolutions");
      return failures;
    }
    const manual = post.resolutions.find((r) => r.speaker_label === "SPEAKER_01");
    const auto = post.resolutions.find((r) => r.speaker_label === "SPEAKER_00");

    if (!manual) {
      failures.push("no resolution for the hand-bound label SPEAKER_01");
    } else if (manual.add_fragment !== true) {
      failures.push(
        `hand-bound voice sent add_fragment=${JSON.stringify(manual.add_fragment)} (expected true) — ` +
        `the person is created but no voice fragment is ever stored`
      );
    }
    if (auto && auto.add_fragment === true) {
      failures.push(
        "confirming an auto-match sent add_fragment=true — that duplicates the fragment " +
        "the person was recognised by and makes the registry grow on every meeting"
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    srv.server.close();
  }
  return failures;
}

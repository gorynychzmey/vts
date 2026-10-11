// A voice nobody has met yet can be named from the task card.
//
// Reported 2026-10-11: in the card, an unrecognised voice could be bound to an
// existing person but there was no way to create a new one — the picker was
// opened with `allowNew: false`, on the grounds that naming belonged to the
// registry dialog. People looked for it in the card, and a voice that stayed
// unnamed is what made "Save & continue" in the other dialog ask about
// anonymous voices.
//
// What this holds: the card's picker offers "new person", and choosing it
// sends ONE save that creates and binds the person (`bind_new` with the name
// and the clip as its first fragment) — the same transaction the dialog uses,
// so a failed save cannot leave a named person with nothing attached. The
// other voices are replayed exactly as they were.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, clickReal } from "../harness.mjs";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static");

export const name = "speaker-panel-creates-person";

const TASK_ID = "88888888-8888-8888-8888-888888888888";
const NEW_NAME = "Николай Дюдяев";

const FLAGS = {
  awaiting_input: {
    is_active: false, is_pending: false, is_finished: false, shows_progress: false,
    can_pause: false, can_resume: true, can_archive: true, needs_input: true,
  },
};

const TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Meeting",
  status: "awaiting_input", awaiting_step: "match_speakers",
  queue: null, queue_position: null, transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [{ name: "match_speakers", status: "failed", started_at: "2026-07-17T10:01:00Z", finished_at: null }],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:01:00Z",
  progress: {}, stats: { media_seconds: 600 },
};

// Diana's task, reduced: one voice the matcher had nobody for (`miss`), one it
// recognised. Only the first needs a new person.
const MATCHES = {
  SPEAKER_00: {
    outcome: "miss", speaker_id: null, distance: null, share: 0.4, seconds: 200, noise: false,
    display_label: "Голос 1", decided_speaker_id: null, decided_name: null, decided_is_noise: null,
    candidates: [{ speaker_id: "sp-a", name: "Диана", distance: 0.71 }],
  },
  SPEAKER_01: {
    outcome: "auto", speaker_id: "sp-a", distance: 0.1, share: 0.6, seconds: 300, noise: false,
    display_label: "Голос 2", decided_speaker_id: null, decided_name: null, decided_is_noise: null,
    candidates: [{ speaker_id: "sp-a", name: "Диана", distance: 0.1 }],
  },
};

export async function run() {
  const failures = [];
  const posts = [];
  const server = http.createServer((req, res) => {
    const [rawPath] = req.url.split("?");
    if (rawPath.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (rawPath === `/api/tasks/${TASK_ID}/speakers` && req.method === "POST") {
        let body = "";
        req.on("data", (c) => { body += c; });
        req.on("end", () => {
          posts.push(JSON.parse(body || "{}"));
          res.end(JSON.stringify({ results: {} }));
        });
        return;
      }
      const table = {
        "/api/status-config": { status_flags: FLAGS },
        "/api/tasks": [TASK],
        [`/api/tasks/${TASK_ID}`]: TASK,
        [`/api/tasks/${TASK_ID}/speaker-matches`]: MATCHES,
        "/api/speakers": [{ id: "sp-a", name: "Диана", sample_count: 1 }],
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/prompts": [],
      };
      res.end(JSON.stringify(rawPath in table ? table[rawPath] : {}));
      return;
    }
    const f = rawPath === "/" ? "/index.html" : rawPath.replace("/static/", "/");
    const fp = path.join(STATIC, f);
    if (!fp.startsWith(STATIC) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") { res.end(fs.readFileSync(fp)); return; }
    const ct = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css" : "text/html";
    res.setHeader("Content-Type", `${ct}; charset=utf-8`);
    res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
  });
  await new Promise((r) => server.listen(0, r));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    page.on("dialog", (d) => (d.type() === "prompt" ? d.accept(NEW_NAME) : d.accept()));
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 8000 });
    await page.click(`[data-task-id="${TASK_ID}"] .task-right-top`);

    // --- SETUP GUARD ------------------------------------------------------
    const unbound = `[data-task-id="${TASK_ID}"] .speaker-box .voice-row-unbound .voice-row-action`;
    const ready = await page.waitForSelector(unbound, { timeout: 5000 }).then(() => true).catch(() => false);
    if (!ready) {
      failures.push("the card's speaker panel showed no unbound voice to name; the rest proves nothing");
      return failures;
    }

    await clickReal(page, unbound);
    const pickerOpen = await page.waitForFunction(
      () => document.getElementById("speaker-picker-dialog")?.open, null, { timeout: 3000 },
    ).then(() => true).catch(() => false);
    if (!pickerOpen) {
      failures.push("the person picker did not open from the card");
      return failures;
    }

    // --- THE ASK: "new person" is offered here ----------------------------
    const newEntry = "#speaker-picker-list .speaker-picker-new";
    if (!(await page.$(newEntry))) {
      failures.push("the card's person picker offers no 'new person' — only existing people can be chosen");
      return failures;
    }
    await clickReal(page, newEntry);
    await page.waitForFunction((n) => n > 0, posts.length, { timeout: 100 }).catch(() => {});
    for (let i = 0; i < 30 && posts.length === 0; i += 1) await page.waitForTimeout(100);

    if (posts.length !== 1) {
      failures.push(`choosing 'new person' sent ${posts.length} saves, expected exactly 1`);
      return failures;
    }
    const byLabel = Object.fromEntries((posts[0].resolutions || []).map((r) => [r.speaker_label, r]));
    const created = byLabel.SPEAKER_00;
    if (!created || created.action !== "bind_new" || created.new_name !== NEW_NAME || created.add_fragment !== true) {
      failures.push(
        `the unrecognised voice should be saved as bind_new "${NEW_NAME}" with its clip; got ${JSON.stringify(created)}`,
      );
    }
    // The recognised voice is replayed, not touched: confirming an auto-match
    // adds no fragment (it was recognised BY one).
    const kept = byLabel.SPEAKER_01;
    if (!kept || kept.action !== "accept_auto" || kept.add_fragment !== false) {
      failures.push(`the other voice should be replayed as accept_auto without a fragment; got ${JSON.stringify(kept)}`);
    }
    // Both voices are now decided, so a task waiting on them should carry on.
    if (posts[0].continue_task !== true) {
      failures.push("with every voice decided the save should continue the task (continue_task=true)");
    }
  } finally {
    await browser.close();
    server.closeAllConnections?.();
    server.close();
  }
  return failures;
}

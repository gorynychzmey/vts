// A slow speaker save must look busy and must not be sent twice.
//
// Prod, 2026-10-11: "Save & continue" in the voice-resolution dialog was
// reported as broken — "it doesn't even close the dialog", while "Save" did.
// It was not broken. A save embeds every voice's clip through the diarization
// sidecar and re-renders the transcript before answering, and that save took
// ~11 s. Nothing on screen said so, so it was clicked four times, 2.5 s apart;
// the dialog closed when the FIRST answer came back, which happened to look
// like the later "Save" click working. Each extra click was a whole new save,
// and every voice of that task collected 3–4 identical fragments.
//
// The server now serialises saves of one task. This scenario holds the
// client's half: while a save is on the wire the buttons are disabled, the
// pressed one says it is saving, further clicks send nothing, and the dialog
// closes once the answer lands. A failed save says so instead of going quiet.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, clickReal, dialogOpen } from "../harness.mjs";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static");

export const name = "voice-save-shows-progress";

const TASK_ID = "77777777-7777-7777-7777-777777777777";
// Long enough to click into, short enough to keep the suite fast.
const SAVE_DELAY_MS = 1500;

const FLAGS = {
  awaiting_input: {
    is_active: false, is_pending: false, is_finished: false, shows_progress: false,
    can_pause: false, can_resume: true, can_archive: true, needs_input: true,
  },
};

const TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Meeting recording",
  status: "awaiting_input", awaiting_step: "match_speakers",
  queue: null, queue_position: null, transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [{ name: "match_speakers", status: "failed", started_at: "2026-07-17T10:01:00Z", finished_at: null }],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:01:00Z",
  progress: {}, stats: { media_seconds: 600 },
};

// Every voice already bound, as reported: no anonymous-voice confirm can be
// the reason the dialog stays open.
const MATCHES = {
  SPEAKER_00: {
    outcome: "auto", speaker_id: "sp-a", distance: 0.1, share: 0.6, seconds: 300, noise: false,
    display_label: "Голос 1", decided_speaker_id: "sp-a", decided_name: "Диана", decided_is_noise: false,
    candidates: [{ speaker_id: "sp-a", name: "Диана", distance: 0.1 }],
  },
  SPEAKER_01: {
    outcome: "auto", speaker_id: "sp-b", distance: 0.2, share: 0.4, seconds: 200, noise: false,
    display_label: "Голос 2", decided_speaker_id: "sp-b", decided_name: "Яна", decided_is_noise: false,
    candidates: [{ speaker_id: "sp-b", name: "Яна", distance: 0.2 }],
  },
};
const SPEAKERS = [
  { id: "sp-a", name: "Диана", sample_count: 1 },
  { id: "sp-b", name: "Яна", sample_count: 1 },
];

function makeServer(state) {
  return http.createServer((req, res) => {
    const [rawPath] = req.url.split("?");
    if (rawPath.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (rawPath === `/api/tasks/${TASK_ID}/speakers` && req.method === "POST") {
        state.posts += 1;
        setTimeout(() => {
          if (state.failSave) {
            res.statusCode = 500;
            res.end(JSON.stringify({ detail: "sidecar unavailable" }));
          } else {
            res.end(JSON.stringify({ results: { SPEAKER_00: "ok", SPEAKER_01: "ok" } }));
          }
        }, SAVE_DELAY_MS);
        return;
      }
      const table = {
        "/api/status-config": { status_flags: FLAGS },
        "/api/tasks": [TASK],
        [`/api/tasks/${TASK_ID}`]: TASK,
        [`/api/tasks/${TASK_ID}/speaker-matches`]: MATCHES,
        "/api/speakers": SPEAKERS,
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
}

async function openResolve(page) {
  await clickReal(page, `[data-task-id="${TASK_ID}"] .task-menu-btn`);
  await page.waitForTimeout(150);
  await clickReal(page, `[data-task-id="${TASK_ID}"] .resolve-voices-btn`);
  return page.waitForFunction(
    () => document.getElementById("voice-resolution-dialog")?.open, null, { timeout: 5000 },
  ).then(() => true).catch(() => false);
}

const buttonState = (page) => page.evaluate(() => {
  const pick = (id) => {
    const b = document.getElementById(id);
    return { disabled: !!b?.disabled, text: (b?.textContent || "").trim() };
  };
  return {
    save: pick("voice-save"), cont: pick("voice-save-continue"), cancel: pick("voice-cancel"),
    saving: window.t ? window.t("voices.dialog.saving") : "",
  };
});

export async function run() {
  const failures = [];
  const state = { posts: 0, failSave: false };
  const server = makeServer(state);
  await new Promise((r) => server.listen(0, r));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 8000 });

    // --- SETUP GUARD ------------------------------------------------------
    if (!(await openResolve(page))) {
      failures.push("the voice dialog never opened; the rest of this scenario proves nothing");
      return failures;
    }

    // --- A. WHILE THE SAVE IS ON THE WIRE ---------------------------------
    await clickReal(page, "#voice-save-continue");
    await page.waitForTimeout(250);
    const busy = await buttonState(page);
    if (!busy.cont.disabled || !busy.save.disabled || !busy.cancel.disabled) {
      failures.push(
        `during a save every button must be disabled; got save=${busy.save.disabled} `
        + `continue=${busy.cont.disabled} cancel=${busy.cancel.disabled}`,
      );
    }
    if (!busy.saving || busy.cont.text !== busy.saving) {
      failures.push(`the pressed button should say ${JSON.stringify(busy.saving)}, says ${JSON.stringify(busy.cont.text)}`);
    }

    // The clicks that produced the duplicates. force: a disabled button is
    // what we are testing, so do not let Playwright wait for it to enable.
    for (let i = 0; i < 3; i += 1) {
      await page.click("#voice-save-continue", { force: true, timeout: 500 }).catch(() => {});
      await page.waitForTimeout(100);
    }
    if (state.posts !== 1) {
      failures.push(`clicking again during a save sent ${state.posts} saves; exactly 1 is allowed`);
    }

    const closed = await page.waitForFunction(
      () => !document.getElementById("voice-resolution-dialog")?.open, null, { timeout: SAVE_DELAY_MS + 4000 },
    ).then(() => true).catch(() => false);
    if (!closed) failures.push("the dialog did not close once the save answered");

    // --- B. A FAILED SAVE SAYS SO -----------------------------------------
    state.failSave = true;
    state.posts = 0;
    if (!(await openResolve(page))) {
      failures.push("could not reopen the dialog for the failure case");
      return failures;
    }
    await clickReal(page, "#voice-save");
    await page.waitForTimeout(SAVE_DELAY_MS + 600);
    const after = await buttonState(page);
    const error = await page.evaluate(() => {
      const el = document.getElementById("voice-dialog-error");
      return el && !el.classList.contains("hidden") && el.offsetHeight > 0 ? el.textContent.trim() : "";
    });
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("a failed save closed the dialog, dropping the user's choices");
    }
    if (!error) {
      failures.push("a failed save showed nothing — the dialog just sat there, as before");
    } else if (!error.includes("sidecar unavailable") && !error.includes("500")) {
      failures.push(`the error line does not carry the reason: ${JSON.stringify(error)}`);
    }
    if (after.save.disabled || after.cont.disabled || after.cancel.disabled) {
      failures.push("after a failed save the buttons stayed disabled; the user cannot retry");
    }
  } finally {
    await browser.close();
    server.closeAllConnections?.();
    server.close();
  }
  return failures;
}

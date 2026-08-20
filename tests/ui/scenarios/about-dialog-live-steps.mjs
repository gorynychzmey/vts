// The About dialog's pipeline steps must follow the task while it is open.
//
// The section was a snapshot taken when the dialog opened, and on an active
// task it drifted further from reality the longer it stayed open — closing and
// reopening was the only cure (vts-4k1e).
//
// Two separate causes, and the second is why "just re-render on the event" is
// not enough:
//   1. The dialog rendered exactly once, from the menu click.
//   2. The handler closed over the `task` object captured when the CARD was
//      built. SSE updates go through patchTaskStep(), which writes to
//      taskEl._runtime.stepStatusByName and never touches task.steps. The two
//      sources of truth had diverged, so re-rendering the captured snapshot
//      would faithfully redraw the same stale rows.
//
// Drives the REAL path: a real EventSource and real `step` events, asserting
// the rows the user sees rather than any internal object.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, launch, dialogOpen, openTaskAbout, settled } from "../harness.mjs";

export const name = "about-dialog-live-steps";

const TASK_ID = "a1111111-1111-1111-1111-111111111111";
const OTHER_ID = "b2222222-2222-2222-2222-222222222222";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  running: { is_active: true, is_pending: false, is_finished: false, shows_progress: true, can_pause: true, can_resume: false, can_archive: false },
};

const step = (name, status, a, b) => ({ name, status, started_at: a, finished_at: b });

// An ACTIVE task: extract_audio running, the rest pending. Only a moving task
// can drift, so a completed one would assert nothing.
const TASK = {
  id: TASK_ID,
  status: "running",
  queue: null, queue_position: null,
  source_url: "https://youtube.com/watch?v=k3Xp9",
  source_title: "Quarterly planning call",
  created_at: "2026-08-17T10:00:00Z", updated_at: "2026-08-17T10:04:13Z",
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: false, language: "", prompts: [] },
  steps: [
    step("download", "completed", "2026-08-17T10:00:00Z", "2026-08-17T10:04:13Z"),
    step("extract_audio", "running", "2026-08-17T10:04:13Z", null),
    step("transcribe_segments", "pending", null, null),
    step("summarize_final", "pending", null, null),
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  progress: {},
  stats: {},
};

async function startServer() {
  const api = { "/api/status-config": { status_flags: FLAGS }, "/api/tasks": [TASK] };
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

async function openPageStreaming(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("EventSource")) {
      errors.push("console.error: " + m.text());
    }
  });
  await page.goto(baseUrl, { waitUntil: "load" });
  // `networkidle` is not usable here: this page holds an SSE connection open,
  // so the network never goes idle. Wait for the app to be genuinely ready
  // instead — the card rendered AND its menu wired — rather than a fixed
  // delay, which lost the race about 1 run in 3.
  await page.waitForSelector(".task .task-menu-btn", { state: "visible" });
  await page.waitForFunction(() => {
    const btn = document.querySelector(".task .task-menu-btn");
    return !!btn && typeof window.EventSource === "function";
  });
  await settled(page);
  return { page, errors };
}

const rowsOf = (page) =>
  page.evaluate(() => {
    const d = document.getElementById("task-about-dialog");
    return [...d.querySelectorAll(".about-step-row")].map((r) => ({
      name: (r.querySelector(".about-step-name")?.textContent || "").trim(),
      state: (r.querySelector(".about-step-state")?.textContent || "").trim(),
      cls: r.className,
    }));
  });

const find = (rows, re) => rows.find((r) => re.test(r.name));

export async function run() {
  const { server, baseUrl, emit } = await startServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPageStreaming(browser, baseUrl);

    try {
      await openTaskAbout(page);
      // openTaskAbout settles on layout, and `openTaskAboutDialog` awaits the
      // prompts cache before showModal() — so on a slow pass the dialog is
      // still opening when settle() returns and `.open` reads false. Wait for
      // the state itself rather than for the page to stop moving.
      await page.waitForFunction(
        () => document.getElementById("task-about-dialog")?.open === true
      );
    } catch (e) {
      failures.push(`About did not open from the kebab menu: ${e.message}`);
      return failures;
    }
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About did not open from the kebab menu");
      return failures;
    }

    const before = await rowsOf(page);
    const extractBefore = find(before, /Audio extraction|extract_audio/i);
    if (!extractBefore) {
      failures.push(`no extract_audio row; saw ${JSON.stringify(before.map((r) => r.name))}`);
      return failures;
    }
    if (!/status-running/.test(extractBefore.cls)) {
      failures.push(`extract_audio should start as running, class was ${JSON.stringify(extractBefore.cls)}`);
    }

    // The pipeline moves on while the dialog stays open.
    emit("step", { task_id: TASK_ID, data: { name: "extract_audio", status: "completed" } });
    emit("step", { task_id: TASK_ID, data: { name: "transcribe_segments", status: "running" } });
    await page.waitForTimeout(400);

    const after = await rowsOf(page);
    const extractAfter = find(after, /Audio extraction|extract_audio/i);
    const transcribeAfter = find(after, /Segment transcription|transcribe_segments/i);

    if (!extractAfter || !/status-completed/.test(extractAfter.cls)) {
      failures.push(`the finished step did not update in the open dialog: ${JSON.stringify(extractAfter)}`);
    }
    if (!transcribeAfter || !/status-running/.test(transcribeAfter.cls)) {
      failures.push(`the newly started step did not update in the open dialog: ${JSON.stringify(transcribeAfter)}`);
    }
    // The visible wording must move too, not just the class.
    if (extractAfter && extractBefore.state === extractAfter.state) {
      failures.push(`the step's outcome text never changed (still ${JSON.stringify(extractAfter.state)})`);
    }

    // A live update must not close the dialog or wipe the facts around the steps.
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("the dialog closed itself on a live update");
    }
    const factRows = await page.$$eval(
      "#task-about-dialog .about-facts .about-row:not(.hidden)",
      (els) => els.length
    );
    if (!factRows) failures.push("the facts table was wiped by the live update");

    // The task's own status line lives in the facts table and went stale the
    // same way. It must follow a task_status event without redrawing the table.
    const statusBefore = await page.$eval("#task-about-dialog .about-status", (el) => el.textContent.trim());
    emit("task_status", { task_id: TASK_ID, data: { status: "completed" } });
    await page.waitForTimeout(400);
    const statusAfter = await page.$eval("#task-about-dialog .about-status", (el) => el.textContent.trim());
    if (statusBefore === statusAfter) {
      failures.push(`the dialog's status line did not follow the task (still ${JSON.stringify(statusAfter)})`);
    }

    // A step update for a DIFFERENT task must not leak into this dialog.
    emit("step", { task_id: OTHER_ID, data: { name: "download", status: "failed" } });
    await page.waitForTimeout(300);
    const leaked = (await rowsOf(page)).some((r) => /status-failed/.test(r.cls));
    if (leaked) failures.push("a step update for another task leaked into this dialog");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

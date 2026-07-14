// Regression (vts-qzl): a `waiting` task (partially processed, active step
// queued in a lane for a slot) must show REAL progress + a "waiting: <lane>"
// label — NOT a queued 0% "in warteschlange". VOS-85 wrongly merged `waiting`
// with `queued` in computeLocalStepProgress/computeOverallProgress and in the
// status badge, so a task 4/12 steps in showed 0% "queued".
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "waiting-lane-progress";

// A summary task partially processed: download/extract/trim/segment completed,
// detect_language pending. status=waiting, queue=gpu (waiting on the GPU lane).
const WAITING_TASK = {
  id: "33333333-3333-3333-3333-333333333333",
  source_url: "http://x/v", source_title: "Waiting video",
  status: "waiting", queue: "gpu", queue_position: 1,
  transcript_path: null, summary_path: null,
  options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
  steps: [
    { name: "download", status: "completed", started_at: "2026-07-14T10:00:00Z", finished_at: "2026-07-14T10:01:00Z" },
    { name: "extract_audio", status: "completed", started_at: "2026-07-14T10:01:00Z", finished_at: "2026-07-14T10:01:30Z" },
    { name: "trim_initial_silence", status: "completed", started_at: "2026-07-14T10:01:30Z", finished_at: "2026-07-14T10:01:40Z" },
    { name: "segment_audio", status: "completed", started_at: "2026-07-14T10:01:40Z", finished_at: "2026-07-14T10:02:00Z" },
    { name: "detect_language", status: "pending" },
  ],
  created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:02:00Z",
  progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } }, stats: {},
};

// A plain queued task (never started) — control: must still show "queue #N".
const QUEUED_TASK = {
  id: "44444444-4444-4444-4444-444444444444",
  source_url: "http://x/q", source_title: "Queued video",
  status: "queued", queue: null, queue_position: 2,
  transcript_path: null, summary_path: null,
  options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
  steps: [],
  created_at: "2026-07-14T10:03:00Z", updated_at: "2026-07-14T10:03:00Z",
  progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } }, stats: {},
};

async function cardTexts(page, taskId) {
  return await page.evaluate((id) => {
    const el = document.querySelector(`[data-task-id="${id}"]`);
    if (!el) return null;
    const q = (sel) => { const n = el.querySelector(sel); return n ? n.textContent.trim() : null; };
    return {
      status: q(".task-status"),
      overall: q(".overall-progress .step-progress-text"),
      local: q(".local-progress .step-progress-text"),
    };
  }, taskId);
}

// "queued/warteschlange/в очереди" wording that must NOT appear for a waiting task.
function looksQueued(text) {
  return /warteschlange|queued|в очереди|queue #/i.test(String(text || ""));
}
function mentionsGpu(text) {
  return /gpu/i.test(String(text || ""));
}

export async function run() {
  const failures = [];
  // The bug is locale-independent (it lives in the progress computation, not in
  // the i18n texts), so one run on the default locale exercises it. Per-locale
  // key parity is covered by the JS syntax check + i18n key set.
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [WAITING_TASK, QUEUED_TASK],
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${WAITING_TASK.id}"]`, { timeout: 5000 });

    const w = await cardTexts(page, WAITING_TASK.id);
    const q = await cardTexts(page, QUEUED_TASK.id);

    if (!w) { failures.push("waiting card not rendered"); return failures; }
    if (!q) { failures.push("queued card not rendered"); return failures; }

    // Waiting task: overall bar must NOT read as queued (it has 4 completed steps).
    if (looksQueued(w.overall)) {
      failures.push(`waiting overall progress reads queued: ${JSON.stringify(w.overall)}`);
    }
    // Waiting task: local step must indicate the GPU lane, not queued.
    if (looksQueued(w.local)) {
      failures.push(`waiting local progress reads queued: ${JSON.stringify(w.local)}`);
    }
    if (!mentionsGpu(w.local) && !mentionsGpu(w.status)) {
      failures.push(`waiting task shows no GPU lane hint (status=${JSON.stringify(w.status)}, local=${JSON.stringify(w.local)})`);
    }
    if (looksQueued(w.status)) {
      failures.push(`waiting status badge reads queued: ${JSON.stringify(w.status)}`);
    }

    // Control: queued task MUST still read as queued (position 2).
    if (!looksQueued(q.overall) && !looksQueued(q.status)) {
      failures.push(`queued task lost its queued wording (status=${JSON.stringify(q.status)}, overall=${JSON.stringify(q.overall)})`);
    }

    if (errors.length) failures.push(`JS errors: ${JSON.stringify(errors)}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

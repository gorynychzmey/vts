import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "status-predicates";

// Mirrors vts.services.task_status.status_flags() exactly (verified against the
// real Python source of truth). The frontend must derive every button's
// enablement from THESE flags, never from its own status literals (vts-c2n).
const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  running:   { is_active:true,  is_pending:false, is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  waiting:   { is_active:true,  is_pending:true,  is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  paused:    { is_active:false, is_pending:false, is_finished:false, shows_progress:false, can_pause:false, can_resume:true,  can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
  failed:    { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:true,  can_archive:true  },
  archived:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
  canceled:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
};

// One task per status, so each button's enablement is checked against the flag.
const IDS = {
  queued:    "11111111-1111-1111-1111-111111111111",
  running:   "22222222-2222-2222-2222-222222222222",
  waiting:   "33333333-3333-3333-3333-333333333333",
  paused:    "44444444-4444-4444-4444-444444444444",
  completed: "55555555-5555-5555-5555-555555555555",
  failed:    "66666666-6666-6666-6666-666666666666",
};

function task(id, status, extra = {}) {
  return {
    id, source_url: "http://x/" + id, source_title: status, status,
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [{ source: "system", id: "summary" }] }, steps: [],
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:00:00Z",
    progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } },
    stats: {}, ...extra,
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": Object.entries(IDS).map(([status, id]) => task(id, status)),
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${IDS.running}"]`, { timeout: 5000 });

    const btn = async (id, sel) => page.evaluate(([i, s]) => {
      const el = document.querySelector(`[data-task-id="${i}"] ${s}`);
      return el
        ? { present: true, disabled: el.disabled === true, hidden: el.classList.contains("hidden") }
        : { present: false };
    }, [id, sel]);

    // A button is "actionable" when present, enabled and not hidden.
    const actionable = (b) => b.present && !b.disabled && !b.hidden;

    // Each button must track its flag for EVERY status: enabled iff the flag is
    // true. This is what catches a status literal drifting from the Python.
    const checks = [
      [".pause-btn", "can_pause"],
      [".resume-btn", "can_resume"],
      [".archive-btn", "can_archive"],
    ];
    for (const [status, id] of Object.entries(IDS)) {
      for (const [sel, flagKey] of checks) {
        const b = await btn(id, sel);
        if (!b.present) { failures.push(`${status}: ${sel} missing`); continue; }
        const want = FLAGS[status][flagKey];
        if (actionable(b) !== want) {
          failures.push(
            `${status}: ${sel} actionable=${actionable(b)} but ${flagKey}=${want}`
          );
        }
      }
    }

    // vts-qzl: a waiting task shows real progress + its lane, never "queued 0%".
    const waitingProgress = await page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .local-progress .step-progress-text`);
      return el ? el.textContent.trim() : null;
    }, IDS.waiting);
    if (waitingProgress === null) {
      failures.push("waiting task: local progress text element not found");
    } else if (/^0%$/.test(waitingProgress)) {
      failures.push(`waiting task shows "${waitingProgress}" (queued 0% regression)`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

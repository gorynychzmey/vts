// VOS-84: cursor-based infinite scroll. A page with fewer tasks than
// tasks_page_size must render all cards, mark itself "exhausted" (the
// #task-sentinel shows the "no more" text, not the spinner), and must not
// produce duplicate cards even if a second page fetch happens to return the
// same rows (the default stub server ignores query strings, so a real
// IntersectionObserver-triggered loadNextPage would ask for the identical
// list again — dedupe in appendTaskCard/prependTaskCard must hold).
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "infinite-scroll-sentinel";

// Mirrors vts.services.task_status.status_flags() (see status-predicates.mjs).
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

const IDS = [
  "a1111111-1111-1111-1111-111111111111",
  "a2222222-2222-2222-2222-222222222222",
  "a3333333-3333-3333-3333-333333333333",
];

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

// Distinct fixed-width ISO created_at values (newest first, matching how the
// server returns the first page), so head/tail cursors are unambiguous.
const TASKS = [
  task(IDS[0], "completed", { created_at: "2026-07-30T12:34:58.000000+00:00" }),
  task(IDS[1], "completed", { created_at: "2026-07-30T12:34:57.000000+00:00" }),
  task(IDS[2], "queued",    { created_at: "2026-07-30T12:34:56.000000+00:00" }),
];

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": TASKS,
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${IDS[0]}"]`, { timeout: 5000 });

    // All 3 cards render.
    for (const id of IDS) {
      const present = await page.evaluate(
        (i) => !!document.querySelector(`[data-task-id="${i}"]`),
        id
      );
      if (!present) failures.push(`task card ${id} did not render`);
    }

    // #task-sentinel exists.
    const sentinelExists = await page.evaluate(() => !!document.getElementById("task-sentinel"));
    if (!sentinelExists) {
      failures.push("#task-sentinel element not found");
      return failures;
    }

    // Give the IntersectionObserver a moment to fire (the sentinel is right
    // below a 3-card list, so it's in view immediately) and for any
    // loadNextPage round-trip to settle.
    await page.waitForTimeout(600);

    // Exhausted (3 < pageSize=10): "no more" text visible, spinner hidden.
    const endVisible = await isVisible(page, ".task-sentinel-end");
    const spinnerVisible = await isVisible(page, ".task-sentinel-spinner");
    if (!endVisible) {
      failures.push("'.task-sentinel-end' (no more) text is not visible though list is exhausted (3 < pageSize 10)");
    }
    if (spinnerVisible) {
      failures.push("'.task-sentinel-spinner' is visible though loading should be false once exhausted");
    }

    // No duplicate cards, even after any loadNextPage that the sentinel's
    // IntersectionObserver may have triggered (the stub server ignores query
    // strings and would return the SAME 3 tasks again for a second page
    // fetch; dedupe in appendTaskCard/prependTaskCard must prevent doubles).
    const counts = await page.evaluate((ids) => {
      return ids.map((i) => document.querySelectorAll(`[data-task-id="${i}"]`).length);
    }, IDS);
    counts.forEach((c, idx) => {
      if (c !== 1) failures.push(`task card ${IDS[idx]} appears ${c} times (expected 1) — dedupe failed`);
    });
    const totalCards = await page.evaluate(() => document.querySelectorAll("#task-list .task").length);
    if (totalCards !== IDS.length) {
      failures.push(`#task-list has ${totalCards} cards, expected exactly ${IDS.length} (no duplicates)`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

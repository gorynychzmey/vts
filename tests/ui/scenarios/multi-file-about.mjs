// vts-vm0: a set's About dialog lists the parts and says how they were ordered.
// The order is decided server-side with no way to correct it, so showing which
// rule produced it is what makes a wrong order explicable.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "multi-file-about";

const TASK_ID = "e2222222-2222-2222-2222-222222222222";

const FLAGS = {
  completed: { is_active:false, is_pending:false, is_finished:true, shows_progress:true,
               can_pause:false, can_resume:false, can_archive:true },
};

const TASK = {
  id: TASK_ID, source_url: "file://part1.m4a", source_title: "Совещание",
  status: "completed", queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: {
    transcript: true, prompts: [],
    source_files: [
      { name: "part1.m4a", offset_sec: 0, duration_sec: 612.4 },
      { name: "part2.m4a", offset_sec: 612.4, duration_sec: 458.1 },
    ],
    source_files_order: "creation_time",
    source_files_kind: "audio",
  },
  steps: [], capabilities: {}, created_at: "2026-08-03T10:00:00.000000+00:00",
  updated_at: "2026-08-03T10:00:00.000000+00:00", progress: {}, stats: {},
};

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": [TASK],
    [`/api/tasks/${TASK_ID}`]: TASK,
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    // The About dialog opens from the card's stats area (app.js:2016), not
    // from a dedicated button.
    await page.click(`[data-task-id="${TASK_ID}"] .task-stats`, { force: true });
    await page.waitForTimeout(400);

    const text = await page.evaluate(() => {
      const dialog = document.getElementById("task-about-dialog");
      return dialog ? dialog.textContent : "";
    });

    if (!text.includes("part1.m4a") || !text.includes("part2.m4a")) {
      failures.push(`About dialog does not list the set's files. Text: ${text.slice(0, 300)}`);
    }
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

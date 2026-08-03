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

// A set that resolved to exactly one part is a real state the server can
// produce (e.g. all-but-one upload rejected). The file list must stay
// hidden in that case, same as an ordinary single-file task.
const SINGLE_TASK_ID = "e3333333-3333-3333-3333-333333333333";
const SINGLE_TASK = {
  ...TASK,
  id: SINGLE_TASK_ID,
  source_url: "file://solo.m4a",
  options: {
    transcript: true, prompts: [],
    source_files: [
      { name: "solo.m4a", offset_sec: 0, duration_sec: 300 },
    ],
    source_files_order: "creation_time",
    source_files_kind: "audio",
  },
};

async function checkDialogText(baseUrl, taskId) {
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${taskId}"]`, { timeout: 5000 });

    // The About dialog opens from the card's stats area (app.js:2016), not
    // from a dedicated button.
    await page.click(`[data-task-id="${taskId}"] .task-stats`, { force: true });
    await page.waitForTimeout(400);

    const result = await page.evaluate(() => {
      const dialog = document.getElementById("task-about-dialog");
      const filesEl = dialog ? dialog.querySelector(".about-source-files") : null;
      return {
        text: dialog ? dialog.textContent : "",
        // Scoped to the file-list element itself, not the whole dialog: the
        // standalone .about-source-url line also renders the first part's
        // filename (it's the upload's source_url), so checking sequence
        // against the FULL dialog text would find that earlier, order-blind
        // occurrence instead of the one inside the list this feature renders.
        filesText: filesEl ? filesEl.textContent : "",
        filesHidden: filesEl ? filesEl.classList.contains("hidden") : null,
      };
    });
    return { ...result, errors };
  } finally {
    await browser.close();
  }
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": [TASK, SINGLE_TASK],
    [`/api/tasks/${TASK_ID}`]: TASK,
    [`/api/tasks/${SINGLE_TASK_ID}`]: SINGLE_TASK,
  });
  try {
    const { text, filesText, errors } = await checkDialogText(baseUrl, TASK_ID);

    if (!text.includes("part1.m4a") || !text.includes("part2.m4a")) {
      failures.push(`About dialog does not list the set's files. Text: ${text.slice(0, 300)}`);
    }
    // Order matters: this is a set whose concat order the user cannot correct,
    // so the list must render in the server-decided sequence, not just mention
    // both names. Checked against filesText (the .about-source-files element
    // alone), not the whole dialog: part1.m4a also appears earlier via the
    // unrelated .about-source-url line regardless of list order, so indexing
    // into the full dialog text would not catch a reversed list.
    const i1 = filesText.indexOf("part1.m4a");
    const i2 = filesText.indexOf("part2.m4a");
    if (i1 === -1 || i2 === -1 || !(i1 < i2)) {
      failures.push(`About dialog does not list files in concat order. Files text: ${filesText.slice(0, 300)}`);
    }
    // The whole point of the feature is naming which rule produced the order.
    // Assert the RESOLVED locale string is present, not just any text, and
    // that the raw i18n key never leaks (which is what a missing/typo'd
    // locale entry would render instead).
    if (!filesText.includes("ordered by recording time")) {
      failures.push(`About dialog does not state the order source. Files text: ${filesText.slice(0, 300)}`);
    }
    if (filesText.includes("about.order_creation_time")) {
      failures.push(`About dialog leaks the raw i18n key instead of the resolved order-source string.`);
    }
    if (errors.length) failures.push("JS errors (multi-file task): " + JSON.stringify(errors));

    const single = await checkDialogText(baseUrl, SINGLE_TASK_ID);
    if (single.filesHidden !== true) {
      failures.push(
        `About dialog shows the file list for a task with exactly one source_files entry ` +
        `(should stay hidden). Text: ${single.text.slice(0, 300)}`
      );
    }
    if (single.errors.length) failures.push("JS errors (single-file-in-set task): " + JSON.stringify(single.errors));
  } finally {
    server.close();
  }
  return failures;
}

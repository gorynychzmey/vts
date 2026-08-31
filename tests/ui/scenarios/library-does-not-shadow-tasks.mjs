// The Library must not make the task list disappear (vts-z3s6).
//
// Reported: stand in the Library, reload, switch to Tasks — empty list, while
// the count still said 97 and "Load more" was offered.
//
// Cause: a recording card is built by renderTaskCard and was given its source
// TASK's id, so it carried the same `data-task-id`. appendTaskCard's dedupe
// guard searched the whole document, so once the Library had loaded — and it
// loads first when it is the remembered view — every task looked "already
// rendered" and none was added. Nothing cleared the list; it was never filled,
// which is why watching for clears found nothing.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "library-does-not-shadow-tasks";

function tasks() {
  return Array.from({ length: 6 }, (_, i) => ({
    id: `1111111${i}-0000-0000-0000-00000000000${i}`,
    source_url: `https://example.com/v${i}`, source_title: `Task ${i}`,
    status: "completed", awaiting_step: null, queue: null, queue_position: null,
    transcript_path: "/t.txt", summary_path: null, redacted_path: null,
    media_path: null,
    options: { transcript: true, prompts: [], prompt_results: [] },
    steps: [], capabilities: {},
    created_at: `2026-08-20T10:0${i}:00Z`, updated_at: "2026-08-20T11:00:00Z",
    progress: {}, stats: {},
  }));
}

export async function run() {
  const failures = [];
  const taskList = tasks();
  // Recordings produced by those very tasks — the ordinary case, not a corner.
  const items = taskList.map((t, i) => ({
    id: `aaaaaaaa-0000-0000-0000-${String(i).padStart(12, "0")}`,
    source_task_id: t.id, title: `Recording ${i}`, title_is_custom: false,
    source_url: t.source_url, duration_sec: 600, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [],
    recorded_at: t.created_at, created_at: t.created_at, updated_at: t.updated_at,
  }));

  const { server, baseUrl } = await startStubServer({
    "/api/tasks": taskList,
    "/api/tasks/count": { total: taskList.length },
    "/api/recordings": { items, total: items.length },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });

    // Load the Library first, which is what a remembered view does on reload.
    await page.click("#main-tab-library");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length > 0,
      null, { timeout: 8000 },
    ).catch(() => {});

    await page.click("#main-tab-tasks");
    await page.waitForTimeout(600);

    const shot = await page.evaluate(() => ({
      taskCards: document.querySelectorAll("#task-list .task").length,
      libraryCards: document.querySelectorAll("#library-list .task").length,
      // No card outside the task list may claim a task id: several lookups key
      // off it, and a stray one silently shadows the real card.
      strayTaskIds: document.querySelectorAll(
        "#library-list [data-task-id]").length,
    }));

    if (shot.taskCards === 0) {
      failures.push(
        "the task list is empty after visiting the Library — the recordings " +
        "are shadowing the tasks"
      );
    }
    if (shot.taskCards !== 6) {
      failures.push(`expected 6 task cards, got ${shot.taskCards}`);
    }
    if (shot.libraryCards !== 6) {
      failures.push(`the library lost its own cards: ${shot.libraryCards}`);
    }
    if (shot.strayTaskIds) {
      failures.push(
        `${shot.strayTaskIds} library cards carry data-task-id; a recording is ` +
        `not a task and must not answer to its id`
      );
    }

    // Two fixes went in, and only one is strictly needed: dropping the
    // attribute is enough on its own (verified by reverting the other half).
    // Scoping findTaskEl to the task list is kept as a second layer, because it
    // protects against ANY future card that carries a task id, not just this
    // one — and it is the assertion above that would catch a regression, so the
    // scoping is documented rather than pinned.

    // And back again: switching must not break either list.
    await page.click("#main-tab-library");
    await page.waitForTimeout(300);
    await page.click("#main-tab-tasks");
    await page.waitForTimeout(500);
    const after = await page.evaluate(
      () => document.querySelectorAll("#task-list .task").length);
    if (after !== 6) {
      failures.push(`after switching twice the task list holds ${after} cards`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

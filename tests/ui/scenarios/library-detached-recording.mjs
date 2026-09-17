// A recording whose task has been deleted, as a row in the Library (vts-0bey).
//
// This is the state the whole recording/task split exists for, and the one the
// card has to get right twice over: it must SAY the job is gone, and it must
// not carry the job's identity. `data-task-id` on a library card is what made
// the task list render empty once (7497036) — the card answered lookups meant
// for a task. A detached recording never had a task id to lend, so the
// attached one is checked beside it: the difference between the two rows is
// the whole behaviour.
//
// `library-hit-orphan` covers the same state reached through SEARCH. This one
// is the plain list, which is how a user meets it.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "library-detached-recording";

const DETACHED_LABEL = "task deleted";

const ATTACHED_ID = "bbbbbbbb-0000-0000-0000-000000000001";
const DETACHED_ID = "bbbbbbbb-0000-0000-0000-000000000002";
const SOURCE_TASK_ID = "cccccccc-0000-0000-0000-000000000001";

function recording(id, sourceTaskId, title) {
  return {
    id,
    source_task_id: sourceTaskId,
    title,
    title_is_custom: false,
    source_url: "file://" + title + ".m4a",
    duration_sec: 630,
    language: "ru",
    tags: [],
    has_transcript: true,
    has_redacted: false,
    has_summary: true,
    has_media: false,
    prompt_results: [],
    recorded_at: "2026-08-20T10:00:00Z",
    created_at: "2026-08-20T10:00:00Z",
    updated_at: "2026-08-20T11:00:00Z",
  };
}

// Job-shaped controls. A recording is not mid-anything and has no job to
// restart, pause or archive, and the player is addressed by the task that may
// be gone.
const JOB_ONLY = [".task-player-btn", ".download-media-btn", ".archive-btn", ".task-status"];

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
    "/api/recordings": {
      items: [
        recording(ATTACHED_ID, SOURCE_TASK_ID, "Attached"),
        recording(DETACHED_ID, null, "Detached"),
      ],
      total: 2,
    },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    // Open the tab before measuring: inside a hidden container every element
    // measures as invisible and every assertion below would pass for free.
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    await page.waitForSelector(`#library-list [data-recording-id="${DETACHED_ID}"]`, {
      timeout: 8000,
    });

    const rows = await page.evaluate((args) => {
      const read = (id) => {
        const el = document.querySelector(`#library-list [data-recording-id="${id}"]`);
        if (!el) return { present: false };
        const visible = {};
        for (const sel of args.jobOnly) {
          const node = el.querySelector(sel);
          visible[sel] = node ? getComputedStyle(node).display !== "none" : false;
        }
        return {
          present: true,
          taskIdAttr: el.getAttribute("data-task-id"),
          sourceTaskIdAttr: el.getAttribute("data-source-task-id"),
          detachedAttr: el.getAttribute("data-detached"),
          cardKind: el.getAttribute("data-card-kind"),
          meta: (el.querySelector(".library-meta")?.textContent || "").trim(),
          visible,
        };
      };
      return { attached: read(args.attached), detached: read(args.detached) };
    }, { attached: ATTACHED_ID, detached: DETACHED_ID, jobOnly: JOB_ONLY });

    for (const [which, row] of Object.entries(rows)) {
      if (!row.present) {
        failures.push(`${which}: the row did not render — nothing below proves anything`);
        continue;
      }
      // Neither kind may answer to a task's id: the lookups that key off it
      // belong to the task list.
      if (row.taskIdAttr !== null) {
        failures.push(
          `${which}: the library card carries data-task-id=${row.taskIdAttr} — ` +
          `task-list lookups will find it`
        );
      }
      if (row.cardKind !== "recording") {
        failures.push(`${which}: data-card-kind is ${row.cardKind}, so the job-shaped chrome is not suppressed`);
      }
      for (const [sel, isVisible] of Object.entries(row.visible)) {
        if (isVisible) failures.push(`${which}: ${sel} is visible on a recording card`);
      }
    }

    if (rows.detached.present) {
      if (rows.detached.detachedAttr !== "1") {
        failures.push("detached: data-detached is not set, so the card cannot be styled or found as one");
      }
      if (!rows.detached.meta.includes(DETACHED_LABEL)) {
        failures.push(
          `detached: the meta line ${JSON.stringify(rows.detached.meta)} does not say ` +
          `${JSON.stringify(DETACHED_LABEL)} — the row looks like any other`
        );
      }
      if (rows.detached.sourceTaskIdAttr !== null) {
        failures.push(
          `detached: data-source-task-id=${rows.detached.sourceTaskIdAttr} on a recording whose task is gone`
        );
      }
    }

    // The control. Without it a card that simply renders nothing would satisfy
    // every check above.
    if (rows.attached.present) {
      if (rows.attached.detachedAttr !== null) {
        failures.push("attached: marked detached although its task is alive");
      }
      if (rows.attached.meta.includes(DETACHED_LABEL)) {
        failures.push(`attached: the meta line claims the task is deleted: ${JSON.stringify(rows.attached.meta)}`);
      }
      if (rows.attached.sourceTaskIdAttr !== SOURCE_TASK_ID) {
        failures.push(
          `attached: data-source-task-id is ${rows.attached.sourceTaskIdAttr}, expected the source task`
        );
      }
      // Setup guard: the meta line is where the detached label would appear,
      // so an empty one would make the check above vacuous.
      if (!rows.attached.meta) {
        failures.push("attached: the meta line is empty — the label checks prove nothing");
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

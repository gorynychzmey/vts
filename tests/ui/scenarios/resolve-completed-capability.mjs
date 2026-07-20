// Verifies the capability-gated visibility of the "Доработать" (resolve
// voices) button (vts-552): it must show on ANY task the backend says can
// re-resolve speakers (capabilities.can_resolve_speakers), not only on a task
// paused at match_speakers. On such a non-paused task the "Save & continue"
// button must be hidden (there's no paused pipeline to continue). Conversely a
// running task before match_speakers, with can_resolve_speakers:false, must
// keep the button hidden.
import {
  startStubServer,
  launch,
  openPage,
  isVisible,
  dialogOpen,
  clickReal,
} from "../harness.mjs";

export const name = "resolve-completed-capability";

const DONE_ID = "11111111-1111-1111-1111-111111111111";
const RUNNING_ID = "22222222-2222-2222-2222-222222222222";

const FLAGS = {
  completed: {
    is_active: false, is_pending: false, is_finished: true, shows_progress: false,
    can_pause: false, can_resume: false, can_archive: true, needs_input: false,
  },
  running: {
    is_active: true, is_pending: false, is_finished: false, shows_progress: true,
    can_pause: true, can_resume: false, can_archive: false, needs_input: false,
  },
};

// Completed task the backend says can re-resolve speakers (edit bindings after
// the fact). NOT awaiting input -> "Save & continue" must be hidden.
const DONE_TASK = {
  id: DONE_ID, source_url: "http://x/done", source_title: "Finished meeting",
  status: "completed", awaiting_step: "",
  queue: null, queue_position: null,
  transcript_path: "/t", summary_path: "/s",
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-17T10:00:00Z", finished_at: "2026-07-17T10:01:00Z" },
    { name: "match_speakers", status: "completed", started_at: "2026-07-17T10:01:00Z", finished_at: "2026-07-17T10:02:00Z" },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false, can_resolve_speakers: true },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:02:00Z",
  progress: {}, stats: { media_seconds: 300 },
};

// Running task, not yet at match_speakers, cannot resolve -> button hidden.
const RUNNING_TASK = {
  id: RUNNING_ID, source_url: "http://x/run", source_title: "In progress",
  status: "running", awaiting_step: "",
  queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "running", started_at: "2026-07-17T10:00:00Z", finished_at: null },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false, can_resolve_speakers: false },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:00:30Z",
  progress: {}, stats: {},
};

const SPEAKER_MATCHES = {
  SPEAKER_00: {
    outcome: "auto", speaker_id: "sp-a", distance: 0.1, share: 0.6, noise: false,
    candidates: [{ speaker_id: "sp-a", name: "Anna", distance: 0.1 }],
  },
};

const ALL_SPEAKERS = [{ id: "sp-a", name: "Anna", sample_count: 2 }];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [DONE_TASK, RUNNING_TASK],
    [`/api/tasks/${DONE_ID}/speaker-matches`]: SPEAKER_MATCHES,
    "/api/speakers": ALL_SPEAKERS,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    await page.waitForSelector(`[data-task-id="${DONE_ID}"]`, { timeout: 5000 });
    await page.waitForSelector(`[data-task-id="${RUNNING_ID}"]`, { timeout: 5000 });

    // --- completed + can_resolve_speakers:true -> button visible ---
    const doneBtn = `[data-task-id="${DONE_ID}"] .resolve-voices-btn`;
    if (!(await isVisible(page, doneBtn))) {
      failures.push("resolve-voices-btn should be visible on a completed task with can_resolve_speakers:true");
    }

    // --- running before match_speakers, can_resolve_speakers:false -> hidden ---
    const runningBtn = `[data-task-id="${RUNNING_ID}"] .resolve-voices-btn`;
    if (await isVisible(page, runningBtn)) {
      failures.push("resolve-voices-btn should be hidden on a running task before match_speakers without can_resolve_speakers");
    }

    // --- open the dialog on the completed task: "Save & continue" hidden
    // (not paused), but "Save" and "Cancel" present ---
    await clickReal(page, doneBtn);
    await page.waitForTimeout(300);
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("dialog did not open from the completed task's resolve button");
      return failures;
    }
    if (await isVisible(page, "#voice-save-continue")) {
      failures.push('"Save & continue" should be hidden when resolving on a non-paused (completed) task');
    }
    if (!(await isVisible(page, "#voice-save"))) {
      failures.push('"Save" should still be visible on a completed task resolve');
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

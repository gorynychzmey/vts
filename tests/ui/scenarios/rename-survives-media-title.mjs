// Regression (vts-hzb): a user rename must survive a media_progress event.
// patchTaskProgress used to unconditionally set runtime.displayName from
// payload.media_title, so the yt-dlp title replaced the custom name in the
// UI the moment download started. The discovered title may only fill an
// EMPTY name.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "rename-survives-media-title";

const RENAMED_ID = "33333333-3333-3333-3333-333333333333";
const UNNAMED_ID = "44444444-4444-4444-4444-444444444444";

function runningTask(id, sourceTitle) {
  return {
    id,
    source_url: "http://x/video",
    source_title: sourceTitle,
    status: "running",
    options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
    steps: [{ name: "download", status: "running", started_at: "2026-07-13T10:00:00Z" }],
    created_at: "2026-07-13T10:00:00Z",
    updated_at: "2026-07-13T10:00:00Z",
    progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } },
    stats: {},
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [runningTask(RENAMED_ID, "Моё имя задачи"), runningTask(UNNAMED_ID, null)],
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${RENAMED_ID}"] .task-link`);

    // White-box shortcut (labeled): the stub server can't stream SSE, so we
    // feed the media_progress payload straight into the app's own handler —
    // the same function the EventSource listener calls. Assertions below are
    // on the observable card title.
    await page.evaluate(([renamedId, unnamedId]) => {
      patchTaskProgress(renamedId, "video", {
        progress: { percent: 5 },
        media_title: "yt-dlp Video Title",
      });
      patchTaskProgress(unnamedId, "video", {
        progress: { percent: 5 },
        media_title: "yt-dlp Video Title",
      });
    }, [RENAMED_ID, UNNAMED_ID]);

    const titleOf = (id) =>
      page.evaluate(
        (sel) => (document.querySelector(sel) || {}).textContent || "",
        `[data-task-id="${id}"] .task-link`
      );

    const renamedTitle = await titleOf(RENAMED_ID);
    if (renamedTitle.trim() !== "Моё имя задачи") {
      failures.push(
        `renamed task title was overwritten by media_title: got ${JSON.stringify(renamedTitle)}, ` +
        `expected "Моё имя задачи"`
      );
    }

    const unnamedTitle = await titleOf(UNNAMED_ID);
    if (unnamedTitle.trim() !== "yt-dlp Video Title") {
      failures.push(
        `unnamed task should adopt media_title: got ${JSON.stringify(unnamedTitle)}, ` +
        `expected "yt-dlp Video Title"`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

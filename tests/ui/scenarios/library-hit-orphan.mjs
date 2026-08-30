// A search hit whose task has been deleted (vts-lib3).
//
// This is the case that motivated reading passages through the RECORDING. The
// player is addressed by task and 404s once the task is gone, so a hit that
// offered only that link would be a dead end for exactly the recordings the
// library exists to keep. The expander must still work, and no player link may
// be offered.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "library-hit-orphan";

const REC_ID = "88888888-8888-8888-8888-888888888888";

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [],
    "/api/recordings": {
      items: [{
        id: REC_ID, source_task_id: null, title: "Archived interview",
        title_is_custom: false, source_url: "file://interview.m4a",
        duration_sec: 900, language: "ru", tags: [],
        has_transcript: true, has_summary: false, has_media: false,
        recorded_at: "2026-06-01T09:00:00Z",
        created_at: "2026-06-01T09:00:00Z", updated_at: "2026-06-01T09:30:00Z",
      }],
      total: 1,
    },
    "/api/search": {
      query: "решение", threshold: 0.45,
      hits: [{
        chunk_id: "99999999-9999-9999-9999-999999999999",
        recording_id: REC_ID,
        // The task is gone. Everything else about the passage is still true.
        source_task_id: null,
        title: "Archived interview",
        text: "тогда и приняли это решение",
        start_sec: 120.0, end_sec: 150.0, speakers: [], score: 0.61,
      }],
    },
    [`/api/recordings/${REC_ID}/transcript`]: {
      recording_id: REC_ID, title: "Archived interview", variant: "raw",
      content: "", around_sec: 120.0,
      entries: [
        { start_sec: 110.0, end_sec: 118.0, text: "перед этим спорили", speaker: null },
        { start_sec: 120.0, end_sec: 150.0, text: "тогда и приняли это решение", speaker: null },
      ],
    },
  });

  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector("#main-tab-library", { timeout: 5000 });
    await page.click("#main-tab-library");
    await page.waitForTimeout(400);

    await page.click("#library-q");
    await page.type("#library-q", "решение");
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-hits-list .library-hit").length > 0,
      null, { timeout: 5000 },
    ).catch(() => {});

    const shape = await page.evaluate(() => {
      const el = document.querySelector("#library-hits-list .library-hit");
      if (!el) return null;
      return {
        playerLink: Boolean(el.querySelector("a[href*='/player/']")),
        hasExpand: Boolean(el.querySelector(".library-hit-expand")),
      };
    });

    if (!shape) {
      failures.push("the search produced no hit at all");
      return failures;
    }
    if (shape.playerLink) {
      failures.push("a dead player link was offered for a deleted task");
    }
    if (!shape.hasExpand) {
      failures.push("no way to read the passage — the hit is a dead end");
    }

    // And the expander genuinely reads through the recording.
    await page.click("#library-hits-list .library-hit-expand");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-hits-list .library-context-line").length > 0,
      null, { timeout: 5000 },
    ).catch(() => {});
    const context = await page.evaluate(() =>
      [...document.querySelectorAll("#library-hits-list .library-context-line")]
        .map((l) => l.textContent).join(" | "));
    if (!context.includes("приняли это решение")) {
      failures.push(`the passage did not open for a detached recording: ${context}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

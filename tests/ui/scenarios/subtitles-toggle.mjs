// vts-fkyq / VOS-128: the raw transcript can be read as running text or as
// SUBTITLES (a WebVTT track). A toggle in the tab actions switches between the
// two views.
//
// What the assertions guard, beyond the happy path:
//   - the toggle belongs to the TRANSCRIPT tab only — other tabs are not timed
//     text, and a control that stays visible there would read as broken;
//   - switching back returns the running text, i.e. the toggle is a view
//     choice, not a one-way conversion;
//   - the pressed state is exposed via aria-pressed, since the two views are
//     only distinguishable by content otherwise.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "subtitles-toggle";

const TASK_ID = "77777777-7777-7777-7777-777777777777";
const PLAIN = "Hello world. This is the running text view.";
const VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\n<v Alice>Hello world";

const TASK = {
  id: TASK_ID, source_url: "file://meeting.webm", source_title: "Meeting recording",
  status: "completed", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: "/t.txt", summary_path: "/s.md", media_path: null,
  options: { transcript: true, diarize: true, prompts: [], prompt_results: [] },
  steps: [],
  capabilities: {},
  created_at: "2026-08-29T10:00:00Z", updated_at: "2026-08-29T11:00:00Z",
  progress: {}, stats: {},
};

const SEL = `[data-task-id="${TASK_ID}"]`;

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK],
    [`/api/tasks/${TASK_ID}/transcript`]: PLAIN,
    [`/api/tasks/${TASK_ID}/subtitles`]: VTT,
    [`/api/tasks/${TASK_ID}/summary`]: "# Summary\n\nbody",
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(SEL, { timeout: 5000 });

    // Expand the card: the tab strip and its actions are built on expand.
    await page.click(`${SEL} .task-right-top`);
    await page.waitForSelector(`${SEL} .tab-content.transcript.active`, { timeout: 5000 });
    await page.waitForFunction(
      (s) => {
        const el = document.querySelector(`${s} .tab-content.transcript`);
        return el && (el.textContent || "").includes("running text");
      },
      SEL,
      { timeout: 5000 },
    );

    const btnSel = `${SEL} .tab-subtitles-btn`;

    // 1. Visible on the transcript tab.
    if (!(await isVisible(page, btnSel))) {
      failures.push("subtitles toggle is not visible on the transcript tab");
      return failures;
    }
    const pressedBefore = await page.evaluate(
      (s) => document.querySelector(s)?.getAttribute("aria-pressed"),
      btnSel,
    );
    if (pressedBefore !== "false") {
      failures.push(`toggle should start unpressed, aria-pressed=${JSON.stringify(pressedBefore)}`);
    }

    // 2. Clicking switches the pane to the WebVTT track.
    await page.click(btnSel);
    await page.waitForFunction(
      (s) => {
        const el = document.querySelector(`${s} .tab-content.transcript`);
        return el && (el.textContent || "").startsWith("WEBVTT");
      },
      SEL,
      { timeout: 5000 },
    ).catch(() => {});
    const subs = await page.evaluate(
      (s) => (document.querySelector(`${s} .tab-content.transcript`)?.textContent || "").trim(),
      SEL,
    );
    if (!subs.startsWith("WEBVTT")) {
      failures.push(`transcript pane did not switch to subtitles, got ${JSON.stringify(subs.slice(0, 60))}`);
    }
    if (!subs.includes("-->")) {
      failures.push("subtitles view has no cue timings");
    }
    const pressedAfter = await page.evaluate(
      (s) => document.querySelector(s)?.getAttribute("aria-pressed"),
      btnSel,
    );
    if (pressedAfter !== "true") {
      failures.push(`toggle should be pressed in subtitles view, aria-pressed=${JSON.stringify(pressedAfter)}`);
    }

    // 3. The toggle belongs to the transcript tab only.
    await page.click(`${SEL} .tab-btn[data-tab="summary"]`);
    await page.waitForTimeout(400);
    if (await isVisible(page, btnSel)) {
      failures.push("subtitles toggle stays visible on the summary tab, where it does nothing");
    }

    // 4. Back on the transcript tab it reappears, and toggling off restores
    //    the running text — the switch is a view, not a conversion.
    await page.click(`${SEL} .tab-btn[data-tab="transcript"]`);
    await page.waitForTimeout(400);
    if (!(await isVisible(page, btnSel))) {
      failures.push("subtitles toggle did not come back on the transcript tab");
    }
    await page.click(btnSel);
    await page.waitForFunction(
      (s) => {
        const el = document.querySelector(`${s} .tab-content.transcript`);
        return el && (el.textContent || "").includes("running text");
      },
      SEL,
      { timeout: 5000 },
    ).catch(() => {});
    const back = await page.evaluate(
      (s) => (document.querySelector(`${s} .tab-content.transcript`)?.textContent || "").trim(),
      SEL,
    );
    if (!back.includes("running text")) {
      failures.push(`toggling off did not restore the running text, got ${JSON.stringify(back.slice(0, 60))}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

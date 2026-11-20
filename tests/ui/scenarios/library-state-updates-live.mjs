// A recording shown in the library updates its state mark in place when its
// task's status changes: "processing" goes away once the task completes,
// without reloading the list (vts-lzad).
//
// The SSE stream is held open and events are pushed into it; a JSON answer on
// /api/events would make the page reload the list on its own and hide a
// missing in-place update.
import { launch } from "../harness.mjs";
import {
  recording, startLibraryLiveServer, lookupsFor, fullLoads, waitUntil, openLibrary,
} from "../library-live-stub.mjs";

export const name = "library-state-updates-live";

const REC = "cccccccc-0000-0000-0000-000000000001";
const TASK = "dddddddd-0000-0000-0000-000000000001";
const OTHER = "cccccccc-0000-0000-0000-000000000002";

export async function run() {
  const failures = [];
  // duration_sec is set so the meta line stays (with "10:30 · RU") once the
  // state label goes: the assertion then reads text, not a missing element.
  const processing = { ...recording(REC, TASK, "2026-08-20T11:00:00Z", "live"), state: "processing" };
  const library = [
    processing,
    recording(OTHER, "dddddddd-0000-0000-0000-000000000002", "2026-08-20T10:00:00Z", "other"),
  ];
  const { server, baseUrl, requests, emit, connected } = await startLibraryLiveServer(library);
  const browser = await launch();
  try {
    const { page, errors } = await openLibrary(browser, baseUrl);
    const sel = `#library-list [data-recording-id="${REC}"]`;
    await page.waitForSelector(sel, { timeout: 8000 });
    if (!(await waitUntil(connected, 5000))) {
      failures.push("setup: the page never opened the SSE stream");
      return failures;
    }
    const before = await page.evaluate((s) => {
      const el = document.querySelector(s);
      el.dataset.marker = "before";
      return el.querySelector(".library-meta")?.textContent || "";
    }, sel);
    if (!before.includes("processing")) {
      failures.push(`setup: the card does not say "processing" (${JSON.stringify(before)})`);
      return failures;
    }

    // The task finishes; the server now reports the recording complete.
    library[0] = { ...processing, state: "complete" };
    emit("task_status", { task_id: TASK, data: { status: "completed" } });
    const cleared = await page.waitForFunction(
      (s) => {
        const meta = document.querySelector(s)?.querySelector(".library-meta");
        return Boolean(meta) && !meta.textContent.includes("processing");
      },
      sel, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!cleared) {
      const now = await page.evaluate((s) => document.querySelector(s)?.querySelector(".library-meta")?.textContent, sel);
      failures.push(`the card still reads ${JSON.stringify(now)} after its task completed`);
    }
    const state = await page.evaluate((s) => {
      const el = document.querySelector(s);
      return {
        marker: el?.dataset.marker || null,
        metas: el ? el.querySelectorAll(".library-meta").length : 0,
        text: el?.querySelector(".library-meta")?.textContent || "",
      };
    }, sel);
    if (state.marker !== "before") failures.push("the card was rebuilt instead of updated in place");
    if (state.metas !== 1) failures.push(`the card carries ${state.metas} meta lines, expected 1`);
    if (cleared && !state.text.includes("RU")) failures.push(`the meta line lost its facts: ${JSON.stringify(state.text)}`);
    const lookups = lookupsFor(requests, TASK).length;
    if (lookups !== 1) failures.push(`one task_status made ${lookups} lookups of the task's recording, expected 1`);
    const loads = fullLoads(requests).length;
    if (loads !== 1) failures.push(`the library list was fetched from scratch ${loads} times, expected 1`);
    const banner = await page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden === false);
    if (banner) failures.push("a shown recording's update raised the new-recordings banner");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

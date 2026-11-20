// The "New recordings" banner must not announce a recording that the list is
// about to show anyway (vts-lzad).
//
// A filter change restarts the library: the list is empty until page 1 comes
// back, so there is no top card to compare with, and "newer than nothing" is
// true for everything. A lookup answered in that window flagged a recording
// that page 1 then rendered — a banner for a card already on screen.
//
// Also checked here: the per-task lookup and the load-newer fetch carry the
// active name filter, so a recording the filter excludes is never announced.
import { launch } from "../harness.mjs";
import {
  recording, startLibraryLiveServer, lookupsFor, waitUntil, openLibrary,
} from "../library-live-stub.mjs";

export const name = "library-banner-filter-race";

const NEW_1 = "aaaaaaaa-0000-0000-0000-000000000011";
const TASK_1 = "bbbbbbbb-0000-0000-0000-000000000011";
const NEW_2 = "aaaaaaaa-0000-0000-0000-000000000012";
const TASK_2 = "bbbbbbbb-0000-0000-0000-000000000012";

const bannerShown = (page) =>
  page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden === false);
const cardCount = (page) =>
  page.evaluate(() => document.querySelectorAll("#library-list .task").length);

export async function run() {
  const failures = [];
  const library = [
    recording("aaaaaaaa-0000-0000-0000-000000000002", "bbbbbbbb-0000-0000-0000-000000000002", "2026-08-20T11:00:00Z", "meeting-a"),
    recording("aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000001", "2026-08-20T10:00:00Z", "meeting-b"),
  ];
  // Holds the first filtered list request until the scenario releases it.
  let release = null;
  let holding = true;
  let held = false;
  const holdList = async (p) => {
    if (!holding || !p.get("q")) return;
    holding = false;
    held = true;
    await new Promise((r) => { release = r; });
  };
  const { server, baseUrl, requests, emit, connected } = await startLibraryLiveServer(library, { holdList });
  const browser = await launch();
  try {
    const { page, errors } = await openLibrary(browser, baseUrl);
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length === 2, null, { timeout: 8000 },
    );
    if (!(await waitUntil(connected, 5000))) {
      failures.push("setup: the page never opened the SSE stream");
      return failures;
    }

    // Change the filter; page 1 of the new query stays in flight.
    await page.fill("#library-q", "meeting");
    if (!(await waitUntil(() => held, 4000))) {
      failures.push("setup: the filter change never requested page 1");
      return failures;
    }
    // Meanwhile a recording enters the library, and its event is answered
    // before page 1 is.
    library.push(recording(NEW_1, TASK_1, "2026-08-20T12:00:00Z", "meeting-new"));
    emit("task_status", { task_id: TASK_1, data: { status: "transcribing" } });
    // Either the lookup is skipped while page 1 is pending, or it runs and must
    // not flag. Give it the debounce and the answer either way.
    await page.waitForTimeout(1000);
    const early = lookupsFor(requests, TASK_1);
    if (early.length && early[0].get("q") !== "meeting") {
      failures.push(`the per-task lookup sent q=${JSON.stringify(early[0].get("q"))}, expected "meeting"`);
    }
    release();
    const rendered = await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length === 3, null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!rendered) failures.push(`page 1 of the filtered list shows ${await cardCount(page)} cards, expected 3`);
    await page.waitForTimeout(400);
    if (await bannerShown(page)) {
      const c = await page.evaluate(() => document.getElementById("new-recordings-count")?.textContent);
      failures.push(`the banner reads ${c} for a recording page 1 already shows`);
    }

    // Now with the list loaded: a newer recording is announced, and both the
    // lookup and the load-newer fetch carry the filter.
    library.push(recording(NEW_2, TASK_2, "2026-08-20T13:00:00Z", "meeting-newer"));
    emit("task_status", { task_id: TASK_2, data: { status: "transcribing" } });
    const up = await page.waitForFunction(
      () => document.getElementById("new-recordings-banner")?.hidden === false, null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!up) {
      failures.push("no banner for a newer recording after the filtered list loaded");
      return failures;
    }
    const lookup = lookupsFor(requests, TASK_2)[0];
    if (lookup?.get("q") !== "meeting") {
      failures.push(`the per-task lookup sent q=${JSON.stringify(lookup?.get("q"))}, expected "meeting"`);
    }
    await page.click("#new-recordings-banner");
    const grew = await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length === 4, null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!grew) failures.push("clicking the banner did not add the newer recording");
    const newer = requests.filter((p) => p.get("after_ts"));
    if (newer.length !== 1) {
      failures.push(`expected one load-newer request, saw ${newer.length}`);
    } else if (newer[0].get("q") !== "meeting") {
      failures.push(`load-newer sent q=${JSON.stringify(newer[0].get("q"))}, expected "meeting"`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    if (release) release();
    await browser.close();
    server.close();
  }
  return failures;
}

// A recording that enters the library while the tab is open is announced by a
// "New recordings (N)" banner, like new tasks are; a click puts it on top
// without rebuilding the cards already there (vts-lzad).
//
// The signals are the existing task events: a task_status or a transcript
// segment for a task with no library card makes the page ask the server for
// that task's recording. Only one that is listed AND newer than the top card
// is announced.
import { launch } from "../harness.mjs";
import {
  recording, startLibraryLiveServer, lookupsFor, fullLoads, waitUntil, openLibrary,
} from "../library-live-stub.mjs";

export const name = "library-new-recordings-banner";

const OLD_A = "aaaaaaaa-0000-0000-0000-000000000002";
const OLD_B = "aaaaaaaa-0000-0000-0000-000000000001";
const NEW_REC = "aaaaaaaa-0000-0000-0000-000000000003";
const NEW_TASK = "bbbbbbbb-0000-0000-0000-000000000003";
// A task whose recording is not in the library (no text yet, or filtered out).
const UNLISTED_TASK = "bbbbbbbb-0000-0000-0000-000000000009";
// A task whose recording IS listed, but far below the top card (beyond the
// loaded pages): looked up once, then never again until the list resets.
const OLDER_TASK = "bbbbbbbb-0000-0000-0000-000000000008";
const FILLERS = 10;

const bannerShown = (page) =>
  page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden === false);

export async function run() {
  const failures = [];
  // Enough cards below the two named ones that the list scrolls in a short
  // viewport: the click must bring the new top card into view.
  const fillers = Array.from({ length: FILLERS }, (_, i) => recording(
    `aaaaaaaa-0000-0000-0001-${String(i).padStart(12, "0")}`,
    `bbbbbbbb-0000-0000-0001-${String(i).padStart(12, "0")}`,
    `2026-08-19T${String(10 + i).padStart(2, "0")}:00:00Z`, `filler-${i}`,
  ));
  const library = [
    recording(OLD_A, "bbbbbbbb-0000-0000-0000-000000000002", "2026-08-20T11:00:00Z", "older-a"),
    recording(OLD_B, "bbbbbbbb-0000-0000-0000-000000000001", "2026-08-20T10:00:00Z", "older-b"),
    ...fillers,
  ];
  const extra = [recording("aaaaaaaa-0000-0000-0000-000000000008", OLDER_TASK, "2026-07-01T10:00:00Z", "ancient")];
  const { server, baseUrl, requests, emit, connected } = await startLibraryLiveServer(library, { extra });
  const initial = library.length;
  const browser = await launch();
  try {
    const { page, errors } = await openLibrary(browser, baseUrl, { width: 1100, height: 500 });
    await page.waitForFunction(
      (n) => document.querySelectorAll("#library-list .task").length === n,
      initial, { timeout: 8000 },
    );
    if (!(await waitUntil(connected, 5000))) {
      failures.push("setup: the page never opened the SSE stream");
      return failures;
    }
    // Mark the existing cards: after the banner click they must be the SAME
    // nodes, not rebuilt copies (a rebuild collapses whatever the user opened).
    await page.evaluate(() => {
      document.querySelectorAll("#library-list .task").forEach((el) => { el.dataset.marker = "before"; });
    });

    // 1. A task whose recording is not listed: looked up, not announced — and
    // looked up again on its next event, since its first text may come later.
    emit("transcript_segment_text", { task_id: UNLISTED_TASK, data: { text: "hello" } });
    if (!(await waitUntil(() => lookupsFor(requests, UNLISTED_TASK).length > 0, 4000))) {
      failures.push("a transcript segment for a task without a card did not look up its recording");
    } else {
      await page.waitForTimeout(400); // let the empty answer be processed
      if (await bannerShown(page)) failures.push("the banner appeared for a task whose recording is not listed");
      emit("transcript_segment_text", { task_id: UNLISTED_TASK, data: { text: "again" } });
      if (!(await waitUntil(() => lookupsFor(requests, UNLISTED_TASK).length === 2, 4000))) {
        failures.push(`a not-yet-listed task was not looked up again (${lookupsFor(requests, UNLISTED_TASK).length} lookups)`);
      }
    }

    // 2. A listed recording older than the top card: not announced, and its
    // later events cost nothing — it can never become newer.
    emit("task_status", { task_id: OLDER_TASK, data: { status: "completed" } });
    if (!(await waitUntil(() => lookupsFor(requests, OLDER_TASK).length > 0, 4000))) {
      failures.push("a task_status for a task without a card did not look up its recording");
    } else {
      await page.waitForTimeout(400);
      emit("task_status", { task_id: OLDER_TASK, data: { status: "archived" } });
      await page.waitForTimeout(1000); // past the debounce and the answer
      const n = lookupsFor(requests, OLDER_TASK).length;
      if (n !== 1) failures.push(`a recording known to be older than the top card was looked up ${n} times, expected 1`);
      if (await bannerShown(page)) failures.push("the banner appeared for a recording older than the top card");
    }

    // 3. A newer recording enters the library. A burst of events for the same
    // task must cost one lookup, not one per event.
    library.push(recording(NEW_REC, NEW_TASK, "2026-08-20T12:00:00Z", "newest"));
    for (let i = 0; i < 4; i += 1) {
      emit("task_status", { task_id: NEW_TASK, data: { status: "transcribing" } });
    }
    const bannerUp = await page.waitForFunction(
      () => {
        const b = document.getElementById("new-recordings-banner");
        return b && !b.hidden && b.offsetParent !== null;
      },
      null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!bannerUp) {
      failures.push("no visible #new-recordings-banner after a newer recording was announced");
      return failures;
    }
    const count = await page.evaluate(() => document.getElementById("new-recordings-count")?.textContent || "");
    if (count !== "(1)") failures.push(`banner count reads ${JSON.stringify(count)}, expected "(1)"`);
    const label = await page.evaluate(() => document.getElementById("new-recordings-banner")?.textContent || "");
    if (!label.includes("New recordings")) failures.push(`banner label reads ${JSON.stringify(label.trim())}`);
    await page.waitForTimeout(700); // past the debounce: a second lookup would have fired by now
    const lookups = lookupsFor(requests, NEW_TASK).length;
    if (lookups !== 1) failures.push(`four events for one task made ${lookups} lookups, expected 1`);

    // 4. Click from further down the page: the new card goes on top and into
    // view, the old ones stay, in order, as the same nodes; the banner goes.
    const scrolled = await page.evaluate(() => {
      window.scrollTo(0, document.body.scrollHeight);
      return window.scrollY;
    });
    if (scrolled <= 0) failures.push("setup: the page does not scroll, so the scroll-to-top check is vacuous");
    // A programmatic click: page.click would scroll the banner into view itself.
    await page.evaluate(() => document.getElementById("new-recordings-banner").click());
    const grew = await page.waitForFunction(
      (n) => document.querySelectorAll("#library-list .task").length === n,
      initial + 1, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!grew) {
      failures.push("clicking the banner did not add the new recording to the list");
      return failures;
    }
    const inView = await page.waitForFunction(
      (id) => {
        const el = document.querySelector(`#library-list [data-recording-id="${id}"]`);
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.top >= 0 && r.top < window.innerHeight;
      },
      NEW_REC, { timeout: 3000 },
    ).then(() => true, () => false);
    if (!inView) failures.push("after the click the new top card is not in the viewport");
    const after = await page.evaluate(() => [...document.querySelectorAll("#library-list .task")].map((el) => ({
      id: el.dataset.recordingId, marker: el.dataset.marker || null,
    })));
    const order = after.slice(0, 3).map((x) => x.id);
    const want = [NEW_REC, OLD_A, OLD_B];
    if (JSON.stringify(order) !== JSON.stringify(want)) {
      failures.push(`list order ${JSON.stringify(order)}, expected ${JSON.stringify(want)}`);
    }
    for (const x of after.slice(1)) {
      if (x.marker !== "before") failures.push(`card ${x.id} was rebuilt instead of kept`);
    }
    if (await bannerShown(page)) failures.push("the banner is still shown after its recordings were loaded");
    const loads = fullLoads(requests).length;
    if (loads !== 1) failures.push(`the library list was fetched from scratch ${loads} times, expected 1`);
    const newer = requests.filter((p) => p.get("after_ts"));
    if (newer.length !== 1) {
      failures.push(`expected one load-newer request, saw ${newer.length}`);
    } else {
      const p = newer[0];
      if (p.get("after_ts") !== "2026-08-20T11:00:00Z" || p.get("after_id") !== OLD_A || p.get("order") !== "asc") {
        failures.push(`load-newer asked for ${p.toString()}, expected after the top card in ascending order`);
      }
    }
    const libCount = await page.evaluate(() => document.getElementById("library-count")?.textContent || "");
    if (libCount !== String(initial + 1)) {
      failures.push(`library count reads ${JSON.stringify(libCount)} after the insert, expected "${initial + 1}"`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

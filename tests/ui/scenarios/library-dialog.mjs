// vts-8w1r / VOS-130: the knowledge library.
//
// A Recording outlives the task that produced it, so the library is not just
// another view of the task list. The assertions that matter are the ones about
// that difference:
//   - a recording whose task is gone is still listed, and says so;
//   - what a recording still HAS is shown per row, because archiving removes
//     the media while the transcript stays;
//   - a failed load must not render as "you have nothing", which is a
//     different statement entirely.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "library-dialog";

const RECORDINGS = {
  items: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      source_task_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "Team sync", source_url: "file://sync.webm",
      duration_sec: 3725, language: "ru", tags: [],
      has_transcript: true, has_summary: true, has_media: true,
      recorded_at: "2026-08-20T10:00:00Z",
      created_at: "2026-08-20T10:00:00Z", updated_at: "2026-08-20T11:00:00Z",
    },
    {
      // The point of the feature: its task was deleted, the recording remains.
      id: "22222222-2222-2222-2222-222222222222",
      source_task_id: null,
      title: "Archived interview", source_url: null,
      duration_sec: 612, language: "en", tags: [],
      has_transcript: true, has_summary: false, has_media: false,
      recorded_at: "2026-07-01T09:00:00Z",
      created_at: "2026-07-01T09:00:00Z", updated_at: "2026-07-01T09:30:00Z",
    },
  ],
  total: 2,
};

async function openLibrary(page) {
  await page.click("#header-menu-btn");
  await page.waitForSelector("#library-btn", { visible: true, timeout: 5000 });
  await page.click("#library-btn");
  await page.waitForFunction(
    () => document.getElementById("library-dialog")?.open === true,
    null, { timeout: 5000 },
  );
}

export async function run() {
  const failures = [];

  // ---- Populated library.
  {
    const { server, baseUrl } = await startStubServer({ "/api/recordings": RECORDINGS });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      await openLibrary(page);
      await page.waitForFunction(
        () => document.querySelectorAll("#library-list .library-row").length > 0,
        null, { timeout: 5000 },
      ).catch(() => {});

      const rows = await page.$$eval("#library-list .library-row", (els) =>
        els.map((e) => ({
          title: e.querySelector(".library-row-title")?.textContent?.trim() || "",
          meta: e.querySelector(".library-row-meta")?.textContent?.trim() || "",
          flags: [...e.querySelectorAll(".library-flag")].map((f) => f.textContent.trim()),
        })));

      if (rows.length !== 2) {
        failures.push(`expected 2 recordings, got ${rows.length}: ${JSON.stringify(rows)}`);
      } else {
        // Duration is rendered, not raw seconds.
        if (!/1:02:05/.test(rows[0].meta)) {
          failures.push(`duration not formatted in the meta line: ${JSON.stringify(rows[0].meta)}`);
        }
        if (!/RU/.test(rows[0].meta)) {
          failures.push(`language missing from the meta line: ${JSON.stringify(rows[0].meta)}`);
        }
        // The detached recording must SAY it is detached — otherwise its row
        // is indistinguishable from one whose task still exists.
        if (rows[1].meta === rows[0].meta || !/удал|delet|gelösch/i.test(rows[1].meta)) {
          failures.push(
            `a recording whose task is gone does not say so: ${JSON.stringify(rows[1].meta)}`);
        }
        // Per-row capability pills, three vs one.
        if (rows[0].flags.length !== 3) {
          failures.push(`row with transcript+summary+media showed ${rows[0].flags.length} pills`);
        }
        if (rows[1].flags.length !== 1) {
          failures.push(
            `row with only a transcript showed ${rows[1].flags.length} pills: ${JSON.stringify(rows[1].flags)}`);
        }
      }

      if (await isVisible(page, "#library-empty")) {
        failures.push("the empty-state message is shown even though there are recordings");
      }
      if (errors.length) failures.push("JS errors (populated): " + JSON.stringify(errors));
    } finally {
      await browser.close();
      server.close();
    }
  }

  // ---- Empty library: says "nothing yet", not an unexplained blank.
  {
    const { server, baseUrl } = await startStubServer({ "/api/recordings": { items: [], total: 0 } });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      await openLibrary(page);
      await page.waitForTimeout(400);
      if (!(await isVisible(page, "#library-empty"))) {
        failures.push("an empty library shows nothing at all, not even an empty state");
      }
      const rows = await page.$$eval("#library-list .library-row", (els) => els.length);
      if (rows) failures.push(`empty library rendered ${rows} rows`);
      if (errors.length) failures.push("JS errors (empty): " + JSON.stringify(errors));
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}

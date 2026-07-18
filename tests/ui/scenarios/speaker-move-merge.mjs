// Verifies the move-fragment and merge-persons UI (vts-552, tasks 10-11):
// a move button per fragment opening a candidate picker ranked by distance
// with "create new" pinned first and a sort toggle; a merge button per person
// whose confirmation names both sides in the right direction; and that the
// picker dialog stays display:none while closed.
import {
  startStubServer,
  launch,
  openPage,
  isVisible,
  dialogOpen,
  clickReal,
  screenshot,
} from "../harness.mjs";

export const name = "speaker-move-merge";

const SPEAKERS = [
  { id: "s1", name: "Vasya-1", sample_count: 2 },
  { id: "s2", name: "Vasya-2", sample_count: 1 },
  { id: "s3", name: "Anna", sample_count: 0 },
];

const SAMPLES_S1 = [
  {
    id: "sm1",
    duration_sec: 5.2,
    source_task_id: null,
    created_at: "2026-07-10T10:00:00Z",
  },
];

// Deliberately NOT in distance order, and "Anna" carries a null distance (no
// comparable fragment) so the client's null-sinks-last rule is observable.
const MOVE_CANDIDATES = [
  { id: "s3", name: "Anna", sample_count: 0, distance: null },
  { id: "s2", name: "Vasya-2", sample_count: 1, distance: 0.12 },
];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/speakers": SPEAKERS,
    "/api/speakers/s1/samples": SAMPLES_S1,
    "/api/speakers/s1/samples/sm1/move-candidates": MOVE_CANDIDATES,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // ANTI-FLICKER: the picker must be display:none before it is ever opened.
    const closedDisplay = await page.evaluate(() => {
      const d = document.getElementById("speaker-picker-dialog");
      return d ? getComputedStyle(d).display : "MISSING";
    });
    if (closedDisplay !== "none") {
      failures.push(
        `closed picker display is ${JSON.stringify(closedDisplay)}, expected "none"`
      );
    }

    await clickReal(page, "#speaker-registry-btn");
    await page.waitForTimeout(250);
    if (!(await dialogOpen(page, "speaker-registry-dialog"))) {
      failures.push("registry dialog did not open");
      return failures;
    }

    // ---- Move a fragment ----
    await clickReal(page, '#speaker-list .speaker-row[data-speaker-id="s1"]');
    await page.waitForTimeout(250);

    const moveBtn = "#speaker-samples .speaker-sample-move-btn";
    if (!(await page.$(moveBtn))) {
      failures.push("no move button on the fragment row");
    } else {
      await clickReal(page, moveBtn);
      await page.waitForTimeout(300);

      if (!(await dialogOpen(page, "speaker-picker-dialog"))) {
        failures.push("picker did not open on move click");
      }
      if (!(await isVisible(page, "#speaker-picker-dialog"))) {
        failures.push("picker not visible after open");
      }

      // "Create new" is pinned first, then candidates nearest-first with the
      // null-distance candidate last.
      const namesDistance = await page.$$eval(
        "#speaker-picker-list .speaker-picker-row .tokens-name",
        (els) => els.map((e) => e.textContent.trim())
      );
      if (namesDistance.length !== 3) {
        failures.push(`expected 3 picker rows, got ${JSON.stringify(namesDistance)}`);
      }
      if (!/^</.test(namesDistance[0] || "")) {
        failures.push(`"create new" is not first: ${JSON.stringify(namesDistance)}`);
      }
      if (namesDistance[1] !== "Vasya-2" || namesDistance[2] !== "Anna") {
        failures.push(
          `distance order wrong (null должен быть последним): ${JSON.stringify(namesDistance)}`
        );
      }

      // The sort toggle must be offered and must actually reorder.
      if (!(await isVisible(page, "#speaker-picker-sort"))) {
        failures.push("sort toggle not visible with candidates present");
      }
      await clickReal(page, "#speaker-picker-sort-alpha");
      await page.waitForTimeout(200);
      const namesAlpha = await page.$$eval(
        "#speaker-picker-list .speaker-picker-row .tokens-name",
        (els) => els.map((e) => e.textContent.trim())
      );
      if (!/^</.test(namesAlpha[0] || "")) {
        failures.push(`"create new" lost its first slot in alpha order: ${JSON.stringify(namesAlpha)}`);
      }
      if (namesAlpha[1] !== "Anna" || namesAlpha[2] !== "Vasya-2") {
        failures.push(`alphabetical order wrong: ${JSON.stringify(namesAlpha)}`);
      }

      // Picking a person confirms, then POSTs the move with that target.
      let moveConfirm = "";
      page.once("dialog", async (dialog) => {
        moveConfirm = dialog.message();
        await dialog.accept();
      });
      const [moveReq] = await Promise.all([
        page.waitForRequest(
          (r) => r.url().includes("/samples/sm1/move") && r.method() === "POST"
        ),
        clickReal(page, '#speaker-picker-list .speaker-picker-row[data-speaker-id="s2"]'),
      ]);
      if (!moveConfirm.includes("Vasya-2")) {
        failures.push(`move confirm does not name the target: ${JSON.stringify(moveConfirm)}`);
      }
      const moveBody = moveReq.postDataJSON();
      if (moveBody.target_speaker_id !== "s2") {
        failures.push(`move POST body wrong: ${JSON.stringify(moveBody)}`);
      }
      await page.waitForTimeout(250);
      if (await dialogOpen(page, "speaker-picker-dialog")) {
        failures.push("picker stayed open after a successful move");
      }
    }

    // ---- Merge two persons ----
    const mergeBtn = '#speaker-list .speaker-row[data-speaker-id="s1"] .speaker-merge-btn';
    if (!(await page.$(mergeBtn))) {
      failures.push("no merge button on the speaker row");
    } else {
      await clickReal(page, mergeBtn);
      await page.waitForTimeout(300);

      if (!(await dialogOpen(page, "speaker-picker-dialog"))) {
        failures.push("picker did not open on merge click");
      }

      // Merge targets an existing person: no "create new", and the source
      // itself must not be offered as its own target.
      const mergeNames = await page.$$eval(
        "#speaker-picker-list .speaker-picker-row .tokens-name",
        (els) => els.map((e) => e.textContent.trim())
      );
      if (mergeNames.some((n) => /^</.test(n))) {
        failures.push(`merge picker offers "create new": ${JSON.stringify(mergeNames)}`);
      }
      if (mergeNames.includes("Vasya-1")) {
        failures.push(`merge picker offers the source itself: ${JSON.stringify(mergeNames)}`);
      }

      // The confirmation must state the direction: source -> target.
      let mergeConfirm = "";
      page.once("dialog", async (dialog) => {
        mergeConfirm = dialog.message();
        await dialog.accept();
      });
      const [mergeReq] = await Promise.all([
        page.waitForRequest(
          (r) => r.url().includes("/api/speakers/s1/merge") && r.method() === "POST"
        ),
        clickReal(page, '#speaker-picker-list .speaker-picker-row[data-speaker-id="s2"]'),
      ]);
      if (!mergeConfirm.includes("Vasya-1") || !mergeConfirm.includes("Vasya-2")) {
        failures.push(`merge confirm misses a name: ${JSON.stringify(mergeConfirm)}`);
      }
      // Direction check: the source must be mentioned before the target, so the
      // user cannot misread which person disappears.
      if (mergeConfirm.indexOf("Vasya-1") > mergeConfirm.indexOf("Vasya-2")) {
        failures.push(`merge confirm reads backwards: ${JSON.stringify(mergeConfirm)}`);
      }
      const mergeBody = mergeReq.postDataJSON();
      if (mergeBody.target_id !== "s2") {
        failures.push(`merge POST body wrong: ${JSON.stringify(mergeBody)}`);
      }
    }

    await screenshot(page, "speaker-move-merge");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Verifies the voice registry dialog (vts-80i, task 13): ONE list with each
// person's fragments expanding inside their own block (redesign v2 replaced the
// two-column layout — a person's fragments used to sit on the far side of the
// dialog, and that side was empty until something was selected), add-speaker form,
// selecting a speaker loads its fragments, inline rename, delete-with-confirm
// naming the fragment count for a speaker, delete-with-confirm for a fragment,
// and that each fragment's <audio> src points at the sample audio endpoint.
import { startStubServer,
  launch,
  openPage,
  isVisible,
  dialogOpen,
  clickReal,
  screenshot, openFromHeaderMenu } from "../harness.mjs";

export const name = "speaker-registry-dialog";

const SPEAKERS = [
  { id: "s1", name: "Vasya", sample_count: 2 },
  { id: "s2", name: "Petya", sample_count: 0 },
];

const SAMPLES_S1 = [
  {
    id: "sm1",
    duration_sec: 5.2,
    source_task_id: "44444444-4444-4444-4444-444444444444",
    created_at: "2026-07-10T10:00:00Z",
  },
  {
    id: "sm2",
    duration_sec: 4.8,
    source_task_id: null,
    created_at: "2026-07-11T10:00:00Z",
  },
];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/speakers": SPEAKERS,
    "/api/speakers/s1/samples": SAMPLES_S1,
    "/api/speakers/s2/samples": [],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // ANTI-FLICKER: closed dialog must be display:none right after boot.
    const closedDisplay = await page.evaluate(() => {
      const d = document.getElementById("speaker-registry-dialog");
      return d ? getComputedStyle(d).display : "MISSING";
    });
    if (closedDisplay !== "none") {
      failures.push(`closed registry dialog display is ${JSON.stringify(closedDisplay)}, expected "none"`);
    }

    if (!(await page.$("#speaker-registry-btn"))) {
      failures.push("no #speaker-registry-btn in header");
      return failures;
    }
    await openFromHeaderMenu(page, "#speaker-registry-btn");
    await page.waitForTimeout(250);
    if (!(await dialogOpen(page, "speaker-registry-dialog"))) {
      failures.push("speaker-registry-dialog did not open on #speaker-registry-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#speaker-registry-dialog"))) {
      failures.push("speaker-registry-dialog not visible after open");
    }

    // One list, both speakers in it.
    const rowCount = await page.$$eval("#speaker-list .speaker-row", (els) => els.length);
    if (rowCount !== 2) failures.push(`expected 2 speaker rows, got ${rowCount}`);

    // Nothing is expanded until a person is opened.
    const panelsBefore = await page.$$eval(".speaker-samples-list", (els) => els.length);
    if (panelsBefore !== 0) failures.push(`no person is open, so no samples panel should exist (got ${panelsBefore})`);

    // --- Adding a speaker ---
    await page.fill("#speaker-create-name", "Kolya");
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/speakers") && r.request().method() === "POST"),
      page.click("#speaker-create-form button[type=submit]"),
    ]);
    await page.waitForTimeout(200);
    // Stub server always 200s writes but GET /api/speakers still returns the
    // static SPEAKERS list, so we only assert the form cleared (real-server
    // behavior — list refresh — is covered by refreshSpeakerRegistry itself).
    const nameAfterSubmit = await page.inputValue("#speaker-create-name");
    if (nameAfterSubmit !== "") failures.push("speaker create form did not clear after submit");

    // --- Selecting a speaker loads its fragments ---
    await clickReal(page, '#speaker-list .speaker-row[data-speaker-id="s1"]');
    await page.waitForTimeout(250);
    // The panel opens INSIDE that person's block, not in a second column.
    const openedInsideBlock = await page.$eval(
      '.person-block[data-speaker-id="s1"]', (el) => !!el.querySelector(".speaker-samples-list")
    ).catch(() => false);
    if (!openedInsideBlock) failures.push("the samples panel must render inside the opened person's block");
    const sampleRowCount = await page.$$eval(".speaker-samples-list .sample-row", (els) => els.length);
    if (sampleRowCount !== 2) failures.push(`expected 2 sample rows for s1, got ${sampleRowCount}`);

    // Playback is a play/stop button backed by one shared Audio() per panel now
    // (five stacked <audio controls> made the panel a wall of widgets). The src
    // only exists after a click, and it must still be a buildPath output — the
    // acting-as param rides on it.
    await clickReal(page, ".speaker-samples-list .sample-row:first-child .play-btn");
    await page.waitForTimeout(300);
    const playedSrc = await page.$eval(".speaker-samples-list", (el) => el._audio?.getAttribute("src") || "");
    if (!playedSrc.includes("/api/speakers/samples/sm1/audio")) {
      failures.push(`play did not point at the sample endpoint, got ${JSON.stringify(playedSrc)}`);
    }
    if (/^https?:/i.test(playedSrc)) {
      failures.push(`sample src must be a relative buildPath output, got ${JSON.stringify(playedSrc)}`);
    }

    // Fragment with a source_task_id shows a clickable link; the other shows
    // plain "source removed" text (no link).
    const sourceInfo = await page.$$eval(".speaker-samples-list .sample-row-name", (els) =>
      els.map((el) => ({ hasLink: !!el.querySelector("a"), text: el.textContent.trim() }))
    );
    if (!sourceInfo.some((s) => s.hasLink)) failures.push("no fragment shows a clickable source-task link");
    if (!sourceInfo.some((s) => !s.hasLink && s.text.length > 0)) {
      failures.push("no fragment shows plain text for a null source_task_id");
    }

    // --- Selecting the other (empty) speaker shows its own empty state ---
    await clickReal(page, '#speaker-list .speaker-row[data-speaker-id="s2"]');
    await page.waitForTimeout(200);
    const s2SampleRows = await page.$$eval(".speaker-samples-list .sample-row", (els) => els.length);
    if (s2SampleRows !== 0) failures.push(`expected 0 sample rows for s2, got ${s2SampleRows}`);
    const s2Empty = await page.$eval(".speaker-samples-list", (el) => el.textContent.trim().length > 0);
    if (!s2Empty) failures.push("empty-samples message missing for a speaker with 0 fragments");

    // --- Inline rename ---
    await clickReal(page, '#speaker-list .speaker-row[data-speaker-id="s1"]');
    await page.waitForTimeout(150);
    const renameBtn = '#speaker-list .speaker-row[data-speaker-id="s1"] [aria-label="Rename"]';
    if (!(await page.$(renameBtn))) failures.push("no rename button on speaker row");
    else {
      await clickReal(page, renameBtn);
      await page.waitForTimeout(150);
      const editingVisible = await page.$eval(
        '#speaker-list .speaker-row[data-speaker-id="s1"] .speaker-name-input',
        (el) => getComputedStyle(el).display !== "none"
      );
      if (!editingVisible) failures.push("rename did not reveal the inline name input");
      await page.fill('#speaker-list .speaker-row[data-speaker-id="s1"] .speaker-name-input', "Vasiliy");
      const [patchReq] = await Promise.all([
        page.waitForRequest(
          (r) => r.url().includes("/api/speakers/s1") && r.method() === "PATCH"
        ),
        page.keyboard.press("Enter"),
      ]);
      const patchBody = patchReq.postDataJSON();
      if (patchBody.name !== "Vasiliy") {
        failures.push(`rename PATCH body wrong: ${JSON.stringify(patchBody)}`);
      }
    }

    // --- Delete speaker: confirmation names the fragment count ---
    let deleteConfirmMsg = "";
    page.once("dialog", async (dialog) => {
      deleteConfirmMsg = dialog.message();
      await dialog.dismiss();
    });
    const deleteSpeakerBtn = '#speaker-list .speaker-row[data-speaker-id="s1"] [aria-label="Delete person"]';
    if (!(await page.$(deleteSpeakerBtn))) failures.push("no delete button on speaker row");
    else {
      await clickReal(page, deleteSpeakerBtn);
      await page.waitForTimeout(150);
      if (!deleteConfirmMsg.includes("2")) {
        failures.push(`delete-speaker confirm does not name fragment count: ${JSON.stringify(deleteConfirmMsg)}`);
      }
    }

    // --- Delete fragment: confirms too ---
    let sampleConfirmSeen = false;
    page.once("dialog", async (dialog) => {
      sampleConfirmSeen = true;
      await dialog.dismiss();
    });
    const deleteSampleBtn = '.speaker-samples-list .sample-row [aria-label="Delete fragment"]';
    if (!(await page.$(deleteSampleBtn))) failures.push("no delete button on sample row");
    else {
      await clickReal(page, deleteSampleBtn);
      await page.waitForTimeout(150);
      if (!sampleConfirmSeen) failures.push("delete-fragment did not show a confirmation dialog");
    }

    await screenshot(page, "speaker-registry-dialog");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

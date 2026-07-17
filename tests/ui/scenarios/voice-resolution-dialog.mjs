// Verifies the voice-resolution dialog (vts-80i, task 14): the "Доработать"
// button on a needs_input task, the three-glyph row rendering (auto/grey/miss),
// the all-speakers-sorted-by-distance dropdown with "<Add new person>"
// positioned per outcome, the three action buttons (save / save&continue /
// cancel) and their confirmations, and the create-form "don't stop for
// review" checkbox's diarize-gated enable state.
import {
  startStubServer,
  launch,
  openPage,
  isVisible,
  dialogOpen,
  clickReal,
  screenshot,
} from "../harness.mjs";

export const name = "voice-resolution-dialog";

const TASK_ID = "77777777-7777-7777-7777-777777777777";

const FLAGS = {
  awaiting_input: {
    is_active: false, is_pending: false, is_finished: false, shows_progress: false,
    can_pause: false, can_resume: true, can_archive: true, needs_input: true,
  },
  running: {
    is_active: true, is_pending: false, is_finished: false, shows_progress: true,
    can_pause: true, can_resume: false, can_archive: false, needs_input: false,
  },
};

const AWAITING_TASK = {
  id: TASK_ID, source_url: "http://x/v", source_title: "Meeting recording",
  status: "awaiting_input", awaiting_step: "match_speakers",
  queue: null, queue_position: null,
  transcript_path: null, summary_path: null,
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-17T10:00:00Z", finished_at: "2026-07-17T10:01:00Z" },
    { name: "match_speakers", status: "failed", started_at: "2026-07-17T10:01:00Z", finished_at: null },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:01:00Z",
  progress: {}, stats: {},
};

const SPEAKER_MATCHES = {
  SPEAKER_00: {
    outcome: "auto",
    speaker_id: "sp-auto",
    distance: 0.12,
    candidates: [
      { speaker_id: "sp-auto", name: "Vasya", distance: 0.12 },
      { speaker_id: "sp-far", name: "Zoya", distance: 0.55 },
    ],
  },
  SPEAKER_01: {
    outcome: "grey",
    speaker_id: null,
    distance: 0.32,
    candidates: [
      { speaker_id: "sp-near", name: "Petya", distance: 0.32 },
      { speaker_id: "sp-far", name: "Zoya", distance: 0.55 },
    ],
  },
  SPEAKER_02: {
    outcome: "miss",
    speaker_id: null,
    distance: null,
    candidates: [],
  },
};

const ALL_SPEAKERS = [
  { id: "sp-auto", name: "Vasya", sample_count: 2 },
  { id: "sp-near", name: "Petya", sample_count: 1 },
  { id: "sp-far", name: "Zoya", sample_count: 3 },
  { id: "sp-unrelated", name: "Andrey", sample_count: 0 },
];

function rowSelector(label) {
  return `#voice-list .voice-row[data-speaker-label="${label}"]`;
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [AWAITING_TASK],
    [`/api/tasks/${TASK_ID}/speaker-matches`]: SPEAKER_MATCHES,
    "/api/speakers": ALL_SPEAKERS,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // ANTI-FLICKER: closed dialog must be display:none right after boot.
    const closedDisplay = await page.evaluate(() => {
      const d = document.getElementById("voice-resolution-dialog");
      return d ? getComputedStyle(d).display : "MISSING";
    });
    if (closedDisplay !== "none") {
      failures.push(`closed voice dialog display is ${JSON.stringify(closedDisplay)}, expected "none"`);
    }

    // --- "Доработать" button renders for a needs_input task ---
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });
    const resolveBtn = `[data-task-id="${TASK_ID}"] .resolve-voices-btn`;
    if (!(await isVisible(page, resolveBtn))) {
      failures.push("resolve-voices-btn not visible on an awaiting_input/match_speakers task");
    }

    // --- opens the dialog ---
    await clickReal(page, resolveBtn);
    await page.waitForTimeout(300);
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("voice-resolution-dialog did not open");
      return failures;
    }

    // --- three rows render, each with the right glyph ---
    const rowCount = await page.$$eval("#voice-list .voice-row", (els) => els.length);
    if (rowCount !== 3) failures.push(`expected 3 voice rows, got ${rowCount}`);

    const glyphs = await page.evaluate((sels) => {
      const out = {};
      for (const [label, sel] of Object.entries(sels)) {
        const el = document.querySelector(sel + " .voice-glyph");
        out[label] = el ? el.textContent : null;
      }
      return out;
    }, {
      SPEAKER_00: rowSelector("SPEAKER_00"),
      SPEAKER_01: rowSelector("SPEAKER_01"),
      SPEAKER_02: rowSelector("SPEAKER_02"),
    });
    if (glyphs.SPEAKER_00 !== "🟢") failures.push(`auto row glyph wrong: ${glyphs.SPEAKER_00}`);
    if (glyphs.SPEAKER_01 !== "🟡") failures.push(`grey row glyph wrong: ${glyphs.SPEAKER_01}`);
    if (glyphs.SPEAKER_02 !== "🔴") failures.push(`miss row glyph wrong: ${glyphs.SPEAKER_02}`);

    // --- dropdown lists ALL speakers, sorted by distance, add-new positioned per outcome ---
    const autoOptions = await page.$$eval(`${rowSelector("SPEAKER_00")} .voice-select option`, (els) =>
      els.map((e) => ({ value: e.value, text: e.textContent }))
    );
    // auto: candidates sorted (Vasya 0.12, Zoya 0.55) then unmatched (Andrey), add-new LAST.
    const autoValues = autoOptions.map((o) => o.value);
    if (autoValues[0] !== "sp-auto") failures.push(`auto row: nearest candidate not first: ${JSON.stringify(autoValues)}`);
    if (autoValues[autoValues.length - 1] !== "__new__") {
      failures.push(`auto row: add-new should be LAST, got ${JSON.stringify(autoValues)}`);
    }
    if (!autoValues.includes("sp-unrelated")) {
      failures.push("auto row: dropdown missing a speaker absent from candidates (must list ALL speakers)");
    }

    const missOptions = await page.$$eval(`${rowSelector("SPEAKER_02")} .voice-select option`, (els) =>
      els.map((e) => e.value)
    );
    if (missOptions[0] !== "__new__") {
      failures.push(`miss row: add-new should be FIRST, got ${JSON.stringify(missOptions)}`);
    }

    // --- preselect: auto/grey -> nearest candidate; miss -> add-new ---
    const preselects = await page.evaluate((sels) => {
      const out = {};
      for (const [label, sel] of Object.entries(sels)) {
        const el = document.querySelector(sel + " .voice-select");
        out[label] = el ? el.value : null;
      }
      return out;
    }, {
      SPEAKER_00: rowSelector("SPEAKER_00"),
      SPEAKER_01: rowSelector("SPEAKER_01"),
      SPEAKER_02: rowSelector("SPEAKER_02"),
    });
    if (preselects.SPEAKER_00 !== "sp-auto") failures.push(`auto row preselect wrong: ${preselects.SPEAKER_00}`);
    if (preselects.SPEAKER_01 !== "sp-near") failures.push(`grey row preselect wrong: ${preselects.SPEAKER_01}`);
    if (preselects.SPEAKER_02 !== "__new__") failures.push(`miss row preselect wrong: ${preselects.SPEAKER_02}`);

    // --- grey/auto rows show the add-fragment checkbox, default ON ---
    const greyFragmentChecked = await page.$eval(
      `${rowSelector("SPEAKER_01")} .voice-add-fragment input[type=checkbox]`,
      (el) => el.checked
    );
    if (!greyFragmentChecked) failures.push("grey row: add-fragment checkbox should default to checked");

    // --- miss row: name input revealed since add-new is preselected ---
    const missNameVisible = await isVisible(page, `${rowSelector("SPEAKER_02")} .voice-new-name`);
    if (!missNameVisible) failures.push("miss row: name input should be visible when add-new is preselected");

    // --- miss row: add-fragment checkbox must be HIDDEN while add-new is
    // selected (it only applies to binding an existing person) ---
    const missFragmentVisible = await isVisible(page, `${rowSelector("SPEAKER_02")} .voice-add-fragment`);
    if (missFragmentVisible) {
      failures.push("miss row: add-fragment checkbox should be hidden while add-new is preselected");
    }

    // --- auto row: name input must be HIDDEN while an existing person is bound ---
    const autoNameVisible = await isVisible(page, `${rowSelector("SPEAKER_00")} .voice-new-name`);
    if (autoNameVisible) failures.push("auto row: name input should be hidden while bound to an existing person");

    await screenshot(page, "voice-resolution-dialog");

    // --- Save: POSTs continue_task=false ---
    const [saveReq] = await Promise.all([
      page.waitForRequest(
        (r) => r.url().includes(`/api/tasks/${TASK_ID}/speakers`) && r.method() === "POST"
      ),
      clickReal(page, "#voice-save"),
    ]);
    const saveBody = saveReq.postDataJSON();
    if (saveBody.continue_task !== false) {
      failures.push(`Save should send continue_task=false, got ${JSON.stringify(saveBody.continue_task)}`);
    }
    if (!Array.isArray(saveBody.resolutions) || saveBody.resolutions.length !== 3) {
      failures.push(`Save resolutions malformed: ${JSON.stringify(saveBody.resolutions)}`);
    }
    await page.waitForTimeout(200);
    if (await dialogOpen(page, "voice-resolution-dialog")) {
      failures.push("dialog still open after Save");
    }

    // --- reopen: Save & continue with an anonymous voice confirms ---
    await clickReal(page, resolveBtn);
    await page.waitForTimeout(250);
    let anonConfirmMsg = "";
    page.once("dialog", async (dialog) => {
      anonConfirmMsg = dialog.message();
      await dialog.dismiss();
    });
    await clickReal(page, "#voice-save-continue");
    await page.waitForTimeout(200);
    if (!anonConfirmMsg) {
      failures.push("Save & continue with the miss row still on add-new (empty name = anonymous) did not confirm");
    } else if (!/Голос 1|Voice 1|Stimme 1/.test(anonConfirmMsg)) {
      failures.push(`anonymous-voice confirm text unexpected: ${JSON.stringify(anonConfirmMsg)}`);
    }

    // Dismissed -> dialog stays open, nothing posted for that click.
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("dialog closed even though the anonymous-voice confirm was dismissed");
    }

    // --- resolve the miss row (bind to an existing speaker), then Save & continue accepts ---
    await page.selectOption(`${rowSelector("SPEAKER_02")} .voice-select`, "sp-unrelated");
    await page.waitForTimeout(100);
    const [contReq] = await Promise.all([
      page.waitForRequest(
        (r) => r.url().includes(`/api/tasks/${TASK_ID}/speakers`) && r.method() === "POST"
      ),
      clickReal(page, "#voice-save-continue"),
    ]);
    const contBody = contReq.postDataJSON();
    if (contBody.continue_task !== true) {
      failures.push(`Save & continue should send continue_task=true, got ${JSON.stringify(contBody.continue_task)}`);
    }
    await page.waitForTimeout(200);
    if (await dialogOpen(page, "voice-resolution-dialog")) {
      failures.push("dialog still open after Save & continue accepted");
    }

    // --- reopen, make a change, Cancel prompts a discard confirm ---
    await clickReal(page, resolveBtn);
    await page.waitForTimeout(250);
    await page.selectOption(`${rowSelector("SPEAKER_00")} .voice-select`, "sp-far");
    await page.waitForTimeout(100);
    let cancelConfirmSeen = false;
    page.once("dialog", async (dialog) => {
      cancelConfirmSeen = true;
      await dialog.dismiss();
    });
    await clickReal(page, "#voice-cancel");
    await page.waitForTimeout(200);
    if (!cancelConfirmSeen) failures.push("Cancel after a dirty edit did not confirm discard");
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("dialog closed even though the discard confirm was dismissed");
    }

    // Accept the discard this time.
    page.once("dialog", async (dialog) => { await dialog.accept(); });
    await clickReal(page, "#voice-cancel");
    await page.waitForTimeout(200);
    if (await dialogOpen(page, "voice-resolution-dialog")) {
      failures.push("dialog still open after accepting the discard confirm");
    }

    // --- reopen with no changes: Cancel closes without any confirm ---
    await clickReal(page, resolveBtn);
    await page.waitForTimeout(250);
    let unexpectedConfirm = false;
    page.once("dialog", async (dialog) => { unexpectedConfirm = true; await dialog.dismiss(); });
    await clickReal(page, "#voice-cancel");
    await page.waitForTimeout(200);
    if (unexpectedConfirm) failures.push("Cancel with no changes should not confirm");
    if (await dialogOpen(page, "voice-resolution-dialog")) {
      failures.push("dialog still open after a clean Cancel");
    }

    // --- create-form checkbox: disabled until diarize is checked ---
    const initialDisabled = await page.$eval("#speaker_no_manual_stop", (el) => el.disabled);
    if (!initialDisabled) failures.push("speaker_no_manual_stop should start disabled (diarize unchecked)");
    await page.click("#diarize");
    await page.waitForTimeout(80);
    const enabledAfterDiarize = await page.$eval("#speaker_no_manual_stop", (el) => !el.disabled);
    if (!enabledAfterDiarize) failures.push("speaker_no_manual_stop did not enable when diarize was checked");
    await page.click("#diarize"); // uncheck again
    await page.waitForTimeout(80);
    const disabledAgain = await page.$eval("#speaker_no_manual_stop", (el) => el.disabled);
    if (!disabledAgain) failures.push("speaker_no_manual_stop did not re-disable when diarize was unchecked");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

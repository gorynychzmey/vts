// Verifies the preset manager dialog: it is HIDDEN when closed (a closed
// <dialog> must keep the UA display:none — a prior bug leaked dialogs visible),
// opens from #presets-btn, renders system + user rows from /api/presets, marks
// the user's default preset, and closes via #presets-close-btn.
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal } from "../harness.mjs";

export const name = "presets-dialog";

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/presets": [
      {
        source: "system",
        id: "default",
        name: "Default",
        editable: false,
        options: { language: "", audio_only: false, transcript: true, prompts: [] },
      },
      {
        source: "user",
        id: "p1",
        name: "Audio memo",
        editable: true,
        options: {
          language: "ru",
          audio_only: true,
          transcript: false,
          prompts: [{ source: "user", id: "u1" }],
        },
      },
    ],
    "/api/me/default_preset": { source: "user", id: "p1" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // CLOSED STATE (critical): dialog hidden before any interaction.
    if (await isVisible(page, "#presets-dialog")) {
      failures.push("presets-dialog VISIBLE before opening (should be hidden)");
    }
    if (await dialogOpen(page, "presets-dialog")) {
      failures.push("presets-dialog reports open=true before opening");
    }

    // Open from the header button.
    if (!(await page.$("#presets-btn"))) {
      failures.push("no #presets-btn in header");
      return failures;
    }
    await clickReal(page, "#presets-btn");
    await page.waitForTimeout(200);
    if (!(await dialogOpen(page, "presets-dialog"))) {
      failures.push("presets-dialog did not open on #presets-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#presets-dialog"))) {
      failures.push("presets-dialog not visible after open");
    }

    // List renders one row per preset (system + user).
    const rowCount = await page.$$eval("#presets-list .prompts-row", (els) => els.length);
    if (rowCount !== 2) {
      failures.push(`expected 2 preset rows, got ${rowCount}`);
    }
    // System badge present, default badge marks the user preset (p1).
    const sys = await page.$$eval("#presets-list .prompt-badge-system", (els) => els.length);
    if (sys !== 1) failures.push(`expected 1 system badge, got ${sys}`);
    const def = await page.$$eval("#presets-list .prompt-badge-default", (els) => els.length);
    if (def !== 1) failures.push(`expected 1 default badge, got ${def}`);

    // User row exposes Edit + Delete; system row does not (count Edit buttons).
    const editBtns = await page.$$eval("#presets-list button", (els) =>
      els.filter((b) => b.textContent.trim() === "Edit").length
    );
    if (editBtns !== 1) failures.push(`expected 1 Edit button (user only), got ${editBtns}`);

    // CREATE MODE (default): the form is visible, submit button reads the
    // create label (not "Edit"), and the prompt multiselect shows rows with
    // the system "summary" prompt checked by default.
    if (!(await isVisible(page, "#preset-form"))) {
      failures.push("#preset-form not visible on open (should be create form by default)");
    }
    const createLabel = (await page.$eval("#preset-submit-btn", (b) => b.textContent.trim()));
    if (createLabel !== "Create preset") {
      failures.push(`expected submit label "Create preset" in create mode, got "${createLabel}"`);
    }
    const cancelHiddenInCreate = await page.$eval("#preset-cancel-btn", (b) => b.classList.contains("hidden"));
    if (!cancelHiddenInCreate) failures.push("cancel button should be hidden in create mode");
    const msRows = await page.$$eval("#preset-edit-prompts .prompt-option, #preset-edit-prompts label", (els) => els.length);
    if (msRows < 1) failures.push(`prompt multiselect shows no rows in create mode (got ${msRows})`);
    const summaryChecked = await page.$$eval("#preset-edit-prompts input[type=checkbox]", (els) =>
      els.filter((c) => c.checked).length
    );
    if (summaryChecked !== 1) failures.push(`expected exactly 1 checked prompt (summary) in create mode, got ${summaryChecked}`);

    // EDIT MODE: click the user preset's Edit -> submit label switches to
    // "Edit" and the multiselect reflects that preset's prompts (u1).
    await page.$$eval("#presets-list button", (els) => {
      const b = els.find((x) => x.textContent.trim() === "Edit");
      if (b) b.click();
    });
    await page.waitForTimeout(150);
    const editLabel = (await page.$eval("#preset-submit-btn", (b) => b.textContent.trim()));
    if (editLabel !== "Edit") {
      failures.push(`expected submit label "Edit" in edit mode, got "${editLabel}"`);
    }
    const editIdVal = await page.$eval("#preset-edit-id", (i) => i.value);
    if (editIdVal !== "p1") failures.push(`expected preset-edit-id "p1" in edit mode, got "${editIdVal}"`);
    const cancelHiddenInEdit = await page.$eval("#preset-cancel-btn", (b) => b.classList.contains("hidden"));
    if (cancelHiddenInEdit) failures.push("cancel button should be visible in edit mode");
    const nameVal = await page.$eval("#preset-name-input", (i) => i.value);
    if (nameVal !== "Audio memo") failures.push(`expected name "Audio memo" in edit mode, got "${nameVal}"`);
    const editChecked = await page.$$eval("#preset-edit-prompts input[type=checkbox]", (els) =>
      els.filter((c) => c.checked).length
    );
    if (editChecked !== 1) failures.push(`expected 1 checked prompt (u1) in edit mode, got ${editChecked}`);

    // Close via the X button.
    await clickReal(page, "#presets-close-btn");
    await page.waitForTimeout(200);
    if (await dialogOpen(page, "presets-dialog")) {
      failures.push("presets-dialog did not close on #presets-close-btn");
    }
    if (await isVisible(page, "#presets-dialog")) {
      failures.push("presets-dialog VISIBLE after close (closed-state leak)");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

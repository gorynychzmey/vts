// Verifies the preset manager dialog: it is HIDDEN when closed (a closed
// <dialog> must keep the UA display:none — a prior bug leaked dialogs visible),
// opens from #presets-btn, renders system + user rows from /api/presets, marks
// the user's default preset, and closes via #presets-close-btn.
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot } from "../harness.mjs";

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
        name: "Standard (Kopie) long name here",
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

    // User row exposes Edit + Delete; system row does not. Buttons are now
    // ICON buttons: identify Edit by its aria-label/title, not text content.
    const editBtns = await page.$$eval("#presets-list button", (els) =>
      els.filter((b) => (b.getAttribute("aria-label") || "") === "Edit preset").length
    );
    if (editBtns !== 1) failures.push(`expected 1 Edit button (user only), got ${editBtns}`);

    // Action buttons are icon buttons, not text buttons.
    const iconBtns = await page.$$eval("#presets-list .prompts-actions .icon-btn", (els) => els.length);
    if (iconBtns < 1) failures.push(`expected .prompts-actions .icon-btn > 0, got ${iconBtns}`);
    const textBtns = await page.$$eval("#presets-list .prompts-actions .btn-text", (els) => els.length);
    if (textBtns !== 0) failures.push(`expected 0 .prompts-actions .btn-text, got ${textBtns}`);

    // The (long) user preset name must NOT be clipped now that icons free up width.
    const presetNameClipped = await page.$$eval("#presets-list .tokens-name", (els) =>
      els.some((el) => el.scrollWidth > el.clientWidth + 1)
    );
    if (presetNameClipped) failures.push("a preset .tokens-name is clipped (scrollWidth > clientWidth)");

    await screenshot(page, "presets-dialog-icon-buttons");

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
    // "Edit preset" and the multiselect reflects that preset's prompts (u1).
    await page.$$eval("#presets-list button", (els) => {
      const b = els.find((x) => (x.getAttribute("aria-label") || "") === "Edit preset");
      if (b) b.click();
    });
    await page.waitForTimeout(150);
    const editLabel = (await page.$eval("#preset-submit-btn", (b) => b.textContent.trim()));
    if (editLabel !== "Edit preset") {
      failures.push(`expected submit label "Edit preset" in edit mode, got "${editLabel}"`);
    }
    const editIdVal = await page.$eval("#preset-edit-id", (i) => i.value);
    if (editIdVal !== "p1") failures.push(`expected preset-edit-id "p1" in edit mode, got "${editIdVal}"`);
    const cancelHiddenInEdit = await page.$eval("#preset-cancel-btn", (b) => b.classList.contains("hidden"));
    if (cancelHiddenInEdit) failures.push("cancel button should be visible in edit mode");
    const nameVal = await page.$eval("#preset-name-input", (i) => i.value);
    if (nameVal !== "Standard (Kopie) long name here") failures.push(`expected name "Standard (Kopie) long name here" in edit mode, got "${nameVal}"`);
    const editChecked = await page.$$eval("#preset-edit-prompts input[type=checkbox]", (els) =>
      els.filter((c) => c.checked).length
    );
    if (editChecked !== 1) failures.push(`expected 1 checked prompt (u1) in edit mode, got ${editChecked}`);

    // No stray horizontal scrollbar, and the row-action tooltips stay inside the
    // dialog. Both had the same cause: a `white-space: nowrap` bubble on a
    // right-edge icon button is wider than its container, so it was clipped by
    // the dialog edge AND inflated the dialog's scrollWidth.
    const overflow = await page.evaluate(() => {
      const d = document.getElementById("presets-dialog");
      return { clientWidth: d.clientWidth, scrollWidth: d.scrollWidth };
    });
    if (overflow.scrollWidth > overflow.clientWidth) {
      failures.push(
        `presets-dialog scrolls horizontally: scrollWidth ${overflow.scrollWidth} > clientWidth ${overflow.clientWidth}`
      );
    }

    const tipFit = await page.evaluate(() => {
      const d = document.getElementById("presets-dialog");
      const btns = [...d.querySelectorAll(".prompts-actions [data-tooltip]")];
      if (!btns.length) return { checked: 0, clipped: [] };
      const dr = d.getBoundingClientRect();
      const clipped = [];
      for (const b of btns) {
        const br = b.getBoundingClientRect();
        const w = parseFloat(getComputedStyle(b, "::after").width);
        // Bubbles here are right-anchored to the button.
        if (br.right - w < dr.left - 1 || br.right > dr.right + 1) {
          clipped.push({ tip: b.getAttribute("data-tooltip"), left: Math.round(br.right - w), dialogLeft: Math.round(dr.left) });
        }
      }
      return { checked: btns.length, clipped };
    });
    if (!tipFit.checked) failures.push("no [data-tooltip] action buttons found in a preset row");
    for (const c of tipFit.clipped) {
      failures.push(`tooltip clipped by the dialog edge: "${c.tip}" starts at ${c.left}, dialog starts at ${c.dialogLeft}`);
    }

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

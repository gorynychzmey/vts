// Verifies the preset manager dialog: it is HIDDEN when closed (a closed
// <dialog> must keep the UA display:none — a prior bug leaked dialogs visible),
// opens from #presets-btn, renders system + user rows from /api/presets, marks
// the user's default preset, and closes via #presets-close-btn.
//
// Redesign v2 rebuilt this dialog as list + editor (the same shape as the
// prompts manager): the rows are SELECTABLE .mgr-item buttons carrying a name
// and a settings summary, and every action (delete, duplicate, make-default)
// moved into the editor and acts on whatever is open there. So the old
// per-row icon buttons are gone, and picking a row is what enters edit mode.
// The [data-tooltip] pattern those buttons hosted moved to the voice registry —
// see tooltip-icon-buttons.
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot, openFromHeaderMenu } from "../harness.mjs";

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
    await openFromHeaderMenu(page, "#presets-btn");
    if (!(await dialogOpen(page, "presets-dialog"))) {
      failures.push("presets-dialog did not open on #presets-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#presets-dialog"))) {
      failures.push("presets-dialog not visible after open");
    }

    // List renders one selectable row per preset (system + user).
    const rowCount = await page.$$eval("#presets-list .mgr-item", (els) => els.length);
    if (rowCount !== 2) {
      failures.push(`expected 2 preset rows, got ${rowCount}`);
    }
    // System badge present, default badge marks the user preset (p1).
    const sys = await page.$$eval("#presets-list .prompt-badge-system", (els) => els.length);
    if (sys !== 1) failures.push(`expected 1 system badge, got ${sys}`);
    const def = await page.$$eval("#presets-list .prompt-badge-default", (els) => els.length);
    if (def !== 1) failures.push(`expected 1 default badge, got ${def}`);

    // The rows carry NO actions — that is the point of the redesign. A row that
    // grows buttons again is the regression this guards.
    const rowBtns = await page.$$eval("#presets-list .mgr-item button, #presets-list .prompts-actions", (els) => els.length);
    if (rowBtns !== 0) failures.push(`preset rows must carry no action buttons, got ${rowBtns}`);

    // A row says what the preset DOES, not just its name — the summary under
    // the name is what makes the list readable without opening each one.
    const withSummary = await page.$$eval("#presets-list .mgr-item .mgr-item-sub", (els) =>
      els.filter((el) => el.textContent.trim().length > 0).length
    );
    if (withSummary < 1) failures.push("no preset row shows a settings summary");

    // A long name is TRUNCATED with an ellipsis, not spilled: the list column is
    // a fixed 13.6rem, so `scrollWidth > clientWidth` is the intended state here
    // (it is what makes the ellipsis appear). What must not happen is the name
    // painting outside its row, which is the actual visual defect.
    const nameOverflow = await page.$$eval("#presets-list .mgr-item-name", (els) =>
      els
        .map((el) => {
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const row = el.closest(".mgr-item").getBoundingClientRect();
          return {
            ellipsis: cs.textOverflow === "ellipsis" && cs.overflow === "hidden",
            escapes: r.right > row.right + 1,
          };
        })
        .filter((x) => !x.ellipsis || x.escapes)
    );
    if (nameOverflow.length) {
      failures.push(`preset name must ellipsis inside its row: ${JSON.stringify(nameOverflow)}`);
    }

    await screenshot(page, "presets-dialog-two-column");

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

    // EDIT MODE: picking the user preset's ROW opens it in the editor -> submit
    // label switches to "Edit preset" and the multiselect reflects its prompts (u1).
    await page.$$eval("#presets-list .mgr-item", (els) => {
      const b = els.find((x) => x.textContent.includes("Standard (Kopie)"));
      if (b) b.click();
    });
    await page.waitForTimeout(200);

    // The picked row is marked, so the list says which preset the editor shows.
    const activeRows = await page.$$eval("#presets-list .mgr-item.active", (els) => els.length);
    if (activeRows !== 1) failures.push(`expected exactly 1 active row after picking, got ${activeRows}`);

    // Actions appear only once something is open, and act on THAT preset.
    const delHidden = await page.$eval("#preset-delete-btn", (b) => b.classList.contains("hidden"));
    if (delHidden) failures.push("delete button should be visible for an open editable preset");
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

    // Every tooltip in the dialog must fit inside it — BOTH edges. The first
    // version of this check only looked at the right-anchored row actions and
    // missed the option-pill bubbles spilling off the LEFT edge.
    const tipFit = await page.evaluate(() => {
      const d = document.getElementById("presets-dialog");
      const els = [...d.querySelectorAll("[data-tooltip]")];
      const dr = d.getBoundingClientRect();
      const clipped = [];
      for (const el of els) {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el, "::after");
        const w = parseFloat(cs.width);
        if (!w) continue;
        // Derive the bubble's box from its anchoring. Note getComputedStyle
        // resolves `left` to a used pixel value even when the rule sets
        // `left: auto`, so key off `transform` (centred bubbles are the only
        // ones translated) and the explicit right/left offsets.
        const centred = cs.transform !== "none";
        let left;
        if (centred) left = r.left + r.width / 2 - w / 2;
        else if (cs.right === "0px") left = r.right - w;   // right-anchored
        else left = r.left;                                // left-anchored
        const right = left + w;
        if (left < dr.left - 1 || right > dr.right + 1) {
          clipped.push({
            tip: (el.getAttribute("data-tooltip") || "").slice(0, 40),
            left: Math.round(left), right: Math.round(right),
            dialog: [Math.round(dr.left), Math.round(dr.right)],
          });
        }
      }
      return { checked: els.length, clipped };
    });
    if (!tipFit.checked) failures.push("no [data-tooltip] elements found in the presets dialog");
    for (const c of tipFit.clipped) {
      failures.push(
        `tooltip clipped by the dialog edge: "${c.tip}" spans ${c.left}..${c.right}, dialog is ${c.dialog[0]}..${c.dialog[1]}`
      );
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

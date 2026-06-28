// Verifies the prompt manager dialog renders rows with ICON action buttons
// (Edit/Delete/Duplicate), not text buttons, so a long prompt name is not
// clipped. Opens from #prompts-btn, asserts the user row name fits, the
// action cluster uses .icon-btn (and zero .btn-text), then screenshots.
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot } from "../harness.mjs";

export const name = "prompts-dialog";

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/prompts": [
      { source: "system", id: "summary", name: "Summary", editable: false },
      { source: "user", id: "u1", name: "Meeting memo with a long enough name to clip", editable: true },
    ],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    if (!(await page.$("#prompts-btn"))) {
      failures.push("no #prompts-btn in header");
      return failures;
    }
    await clickReal(page, "#prompts-btn");
    await page.waitForTimeout(200);
    if (!(await dialogOpen(page, "prompts-dialog"))) {
      failures.push("prompts-dialog did not open on #prompts-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#prompts-dialog"))) {
      failures.push("prompts-dialog not visible after open");
    }

    // Rows render (system + user).
    const rowCount = await page.$$eval("#prompts-list .prompts-row", (els) => els.length);
    if (rowCount !== 2) failures.push(`expected 2 prompt rows, got ${rowCount}`);

    // Action buttons are icon buttons, not text buttons.
    const iconBtns = await page.$$eval("#prompts-list .prompts-actions .icon-btn", (els) => els.length);
    if (iconBtns < 1) failures.push(`expected .prompts-actions .icon-btn > 0, got ${iconBtns}`);
    const textBtns = await page.$$eval("#prompts-list .prompts-actions .btn-text", (els) => els.length);
    if (textBtns !== 0) failures.push(`expected 0 .prompts-actions .btn-text, got ${textBtns}`);

    // Edit/Delete/Duplicate carry localized tooltips via aria-label.
    const editBtns = await page.$$eval("#prompts-list button", (els) =>
      els.filter((b) => (b.getAttribute("aria-label") || "") === "Edit").length
    );
    if (editBtns !== 1) failures.push(`expected 1 Edit icon button (user only), got ${editBtns}`);

    // The (long) prompt name must NOT be clipped.
    const nameClipped = await page.$$eval("#prompts-list .tokens-name", (els) =>
      els.some((el) => el.scrollWidth > el.clientWidth + 1)
    );
    if (nameClipped) failures.push("a prompt .tokens-name is clipped (scrollWidth > clientWidth)");

    await screenshot(page, "prompts-dialog-icon-buttons");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

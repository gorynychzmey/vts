// Verifies the prompt manager dialog.
//
// The original defect (vts): action buttons crowded the row and clipped a long
// prompt name. Redesign v2 solved it structurally — the rows have no actions at
// all now. Picking a row opens it in the editor beside the list, and
// Delete/Duplicate/Save act on whatever is open there. So the assertion moved
// from "the actions are icons, not text" to what actually mattered: the NAME is
// what identifies a row, and it must not be clipped.
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot, openFromHeaderMenu } from "../harness.mjs";

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
    await openFromHeaderMenu(page, "#prompts-btn");
    await page.waitForTimeout(200);
    if (!(await dialogOpen(page, "prompts-dialog"))) {
      failures.push("prompts-dialog did not open on #prompts-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#prompts-dialog"))) {
      failures.push("prompts-dialog not visible after open");
    }

    // Rows render (system + user).
    const rowCount = await page.$$eval("#prompts-list .mgr-item", (els) => els.length);
    if (rowCount !== 2) failures.push(`expected 2 prompt rows, got ${rowCount}`);

    // The row carries no actions: they belong to the editor, which acts on the
    // open prompt. A row that grew buttons again would clip the name once more.
    const rowBtns = await page.$$eval("#prompts-list .mgr-item button", (els) => els.length);
    if (rowBtns !== 0) failures.push(`prompt rows must carry no action buttons, got ${rowBtns}`);

    // The NAME identifies a row — Victor's call — with the body's first line as
    // a preview underneath, not as the headline.
    const rows = await page.$$eval("#prompts-list .mgr-item", (els) =>
      els.map((el) => ({
        name: el.querySelector(".mgr-item-name")?.textContent || "",
        clipped: (() => {
          const n = el.querySelector(".mgr-item-name");
          return n ? n.scrollWidth > n.clientWidth + 1 : false;
        })(),
      }))
    );
    if (!rows.some((r) => r.name.startsWith("Meeting memo"))) {
      failures.push(`the user prompt's NAME must be the row's headline, got ${JSON.stringify(rows.map((r) => r.name))}`);
    }
    // Ellipsis is fine; overflowing the column is not — that was the original bug.
    const overflowing = await page.$$eval("#prompts-list .mgr-item", (els) =>
      els.filter((el) => el.scrollWidth > el.clientWidth + 1).length
    );
    if (overflowing) failures.push(`${overflowing} prompt row(s) overflow the list column`);

    // Picking a row loads it into the editor beside the list.
    await clickReal(page, "#prompts-list .mgr-item:last-child");
    await page.waitForTimeout(300);
    const editorName = await page.inputValue("#prompt-name-input");
    if (!editorName.startsWith("Meeting memo")) {
      failures.push(`clicking a row must open it in the editor, got ${JSON.stringify(editorName)}`);
    }

    await screenshot(page, "prompts-dialog-icon-buttons");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

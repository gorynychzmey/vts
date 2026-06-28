// Verifies the create-form prompt selector: the popover is hidden by default,
// opens when the toggle is clicked, and closes on an outside click. (The popover
// once shipped always-visible because a class rule overrode [hidden].)
import { startStubServer, launch, openPage, isVisible, clickReal } from "../harness.mjs";

export const name = "prompt-select";

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // The selector container renders.
    if (!(await page.$("#prompt-select .prompt-select-toggle"))) {
      failures.push("no prompt-select toggle in create form");
      return failures;
    }
    // CLOSED STATE: popover hidden before any interaction.
    if (await isVisible(page, "#prompt-select .prompt-select-popover")) {
      failures.push("prompt-select popover VISIBLE before opening (should be hidden)");
    }
    // Open via toggle.
    await clickReal(page, "#prompt-select .prompt-select-toggle");
    await page.waitForTimeout(150);
    if (!(await isVisible(page, "#prompt-select .prompt-select-popover"))) {
      failures.push("popover did not open on toggle click");
    }
    // Close via outside click (click the page header).
    await clickReal(page, "h1");
    await page.waitForTimeout(150);
    if (await isVisible(page, "#prompt-select .prompt-select-popover")) {
      failures.push("popover did not close on outside click");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

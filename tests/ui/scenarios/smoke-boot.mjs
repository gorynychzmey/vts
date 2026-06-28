// Boots the app, asserts the page loaded with no JS errors and the create form is present.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "smoke-boot";

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));
    if (!(await isVisible(page, "#task-form"))) failures.push("#task-form not visible after boot");
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

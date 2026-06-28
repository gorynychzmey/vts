// Boots the app with /api/uploads/config stubbed; asserts the app boots cleanly,
// proving loadUploadConfig() doesn't break bootstrap.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "chunked-upload";

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/uploads/config": {
      chunked_threshold_bytes: 52428800,
      chunk_bytes: 8388608,
      max_upload_bytes: 2147483648,
    },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (!(await isVisible(page, "#task-form"))) {
      failures.push("app did not boot (#task-form missing) with uploads/config stubbed");
    }
    if (errors.length) failures.push(`console errors: ${errors.join("; ")}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

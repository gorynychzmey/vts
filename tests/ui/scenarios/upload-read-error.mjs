// When the selected file becomes unreadable (NotReadableError — file modified,
// moved, or a cloud placeholder), the form must show a localized error instead
// of an uncaught promise rejection in the console.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "upload-read-error";

const FILE = { name: "test.mp4", mimeType: "video/mp4", buffer: Buffer.from("x".repeat(100)) };

async function selectFile(page) {
  // The radio input is visually hidden (styled toggle) — switch it directly.
  await page.evaluate(() => {
    const radio = document.getElementById("source-type-file");
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.setInputFiles("#file-input", FILE);
}

// Make Blob.arrayBuffer throw Chrome's NotReadableError starting from call
// number `failFrom` (1-based), so both the pre-upload probe (call 1) and the
// per-chunk read (call 2+) paths can be exercised.
async function breakFileReads(page, failFrom) {
  await page.evaluate((n) => {
    let calls = 0;
    Blob.prototype.arrayBuffer = function () {
      calls += 1;
      if (calls >= n) {
        return Promise.reject(new DOMException(
          "The requested file could not be read, typically due to permission problems that have occurred after a reference to a file was acquired.",
          "NotReadableError",
        ));
      }
      return Promise.resolve(new ArrayBuffer(this.size));
    };
  }, failFrom);
}

async function checkErrorShown(page, failures, label) {
  await page.waitForTimeout(300);
  if (!(await isVisible(page, "#task-form-error"))) {
    failures.push(`${label}: #task-form-error not visible after failed read`);
    return;
  }
  const text = await page.$eval("#task-form-error", (el) => el.textContent);
  if (!/could not be read|select the file again/i.test(text)) {
    failures.push(`${label}: unexpected error text: "${text}"`);
  }
  const btnDisabled = await page.$eval("#submit-btn", (el) => el.disabled);
  if (btnDisabled) {
    failures.push(`${label}: submit button left disabled after failure`);
  }
}

export async function run() {
  const failures = [];
  const browser = await launch();
  try {
    // Case 1: single-shot path (file below chunked threshold) — the
    // readability probe before upload must catch the dead file reference.
    {
      const { server, baseUrl } = await startStubServer({
        "/api/uploads/config": {
          chunked_threshold_bytes: 52428800,
          chunk_bytes: 8388608,
          max_upload_bytes: 2147483648,
        },
      });
      try {
        const { page, errors } = await openPage(browser, baseUrl);
        await selectFile(page);
        await breakFileReads(page, 1);
        await page.click("#submit-btn");
        await checkErrorShown(page, failures, "single-shot");
        if (errors.length) failures.push(`single-shot: console errors: ${errors.join("; ")}`);
      } finally {
        server.close();
      }
    }

    // Case 2: chunked path — probe succeeds, the first chunk read fails.
    {
      const { server, baseUrl } = await startStubServer({
        "/api/uploads/config": {
          chunked_threshold_bytes: 10,
          chunk_bytes: 8388608,
          max_upload_bytes: 2147483648,
        },
      });
      try {
        const { page, errors } = await openPage(browser, baseUrl);
        await selectFile(page);
        await breakFileReads(page, 2);
        await page.click("#submit-btn");
        await checkErrorShown(page, failures, "chunked");
        if (errors.length) failures.push(`chunked: console errors: ${errors.join("; ")}`);
      } finally {
        server.close();
      }
    }
  } finally {
    await browser.close();
  }
  return failures;
}

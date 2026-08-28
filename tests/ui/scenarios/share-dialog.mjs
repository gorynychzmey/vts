// vts-qv6l / VOS-127: sharing a result.
//
// The dialog asks ONE question — which artifact — and hands it to the OS share
// sheet as a FILE. There is no "via Telegram / WhatsApp / mail" axis on purpose:
// navigator.share({files}) opens the system sheet where the user picks the app,
// and t.me/wa.me/mailto take text in a URL and cannot carry a file (transcripts
// run 83-159 KB on prod).
//
// Both branches are asserted, because the fallback is the one users on desktop
// will actually hit:
//   - a browser that CAN share files gets navigator.share({files}) with a real
//     File, and no download;
//   - a browser that CANNOT gets a download, and is told so BEFORE pressing
//     Share rather than being surprised afterwards.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "share-dialog";

const TASK_ID = "66666666-6666-6666-6666-666666666666";
const PROMPT_ID = "u1";
const TRANSCRIPT = "Hello world. Running text.";

const TASK = {
  id: TASK_ID, source_url: "file://meeting.webm", source_title: "Meeting recording",
  status: "completed", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: "/t.txt", summary_path: "/s.md", redacted_path: "/r.txt",
  media_path: null,
  options: {
    transcript: true, diarize: false,
    prompts: [{ source: "user", id: PROMPT_ID }],
    prompt_results: [{ source: "user", id: PROMPT_ID, name: "Memo", status: "completed" }],
  },
  steps: [], capabilities: {},
  created_at: "2026-08-29T10:00:00Z", updated_at: "2026-08-29T11:00:00Z",
  progress: {}, stats: {},
};

const SEL = `[data-task-id="${TASK_ID}"]`;

// Replace navigator.share/canShare before the app runs, and record the calls.
// `supported: false` models a desktop browser without file sharing.
async function installShareSpy(page, supported) {
  await page.addInitScript((ok) => {
    window.__shareCalls = [];
    window.__downloads = [];
    Object.defineProperty(navigator, "canShare", {
      configurable: true,
      value: () => ok,
    });
    Object.defineProperty(navigator, "share", {
      configurable: true,
      value: async (data) => {
        const files = (data && data.files) || [];
        window.__shareCalls.push({
          title: data && data.title,
          files: files.map((f) => ({ name: f.name, type: f.type, size: f.size })),
        });
      },
    });
    // Downloads go through an <a download>.click(); record instead of navigating.
    const nativeClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function patched() {
      if (this.hasAttribute("download")) {
        window.__downloads.push(this.getAttribute("download"));
        return;
      }
      return nativeClick.apply(this, arguments);
    };
  }, supported);
}

async function openCardAndDialog(page) {
  await page.waitForSelector(SEL, { timeout: 5000 });
  await page.click(`${SEL} .task-right-top`);
  await page.waitForSelector(`${SEL} .tab-content.transcript.active`, { timeout: 5000 });
  await page.click(`${SEL} .tab-share-btn`);
  await page.waitForFunction(
    () => document.getElementById("share-dialog")?.open === true,
    null,
    { timeout: 5000 },
  );
}

function stubs() {
  return {
    "/api/tasks": [TASK],
    "/api/prompts": [{ source: "user", id: PROMPT_ID, name: "Memo", editable: true }],
    [`/api/tasks/${TASK_ID}/transcript`]: TRANSCRIPT,
    [`/api/tasks/${TASK_ID}/subtitles`]: "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi",
    [`/api/tasks/${TASK_ID}/redacted`]: "redacted text",
    [`/api/tasks/${TASK_ID}/summary`]: "# Summary",
    [`/api/tasks/${TASK_ID}/results/user/${PROMPT_ID}`]: "memo body",
  };
}

export async function run() {
  const failures = [];

  // ---- Branch 1: the browser CAN share files.
  {
    const { server, baseUrl } = await startStubServer(stubs());
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      await installShareSpy(page, true);
      await page.reload({ waitUntil: "domcontentloaded" });
      await openCardAndDialog(page);

      // The dialog offers the artifacts, and NOT the log (diagnostics, not a
      // result a person is shared).
      const labels = await page.$$eval("#share-options .share-option span",
        (els) => els.map((e) => (e.textContent || "").trim()));
      if (labels.length < 2) {
        failures.push(`share dialog listed too few options: ${JSON.stringify(labels)}`);
      }
      if (labels.some((l) => /^log$/i.test(l))) {
        failures.push(`the log is offered as a share option: ${JSON.stringify(labels)}`);
      }
      if (!labels.some((l) => /memo/i.test(l))) {
        failures.push(`the user prompt's result is missing from the options: ${JSON.stringify(labels)}`);
      }

      // No axis of messenger buttons — that choice belongs to the system sheet.
      const svcButtons = await page.$$eval("#share-dialog button",
        (els) => els.map((e) => (e.textContent || "").trim()).join(" "));
      if (/telegram|whatsapp/i.test(svcButtons)) {
        failures.push("the dialog offers per-service buttons, which cannot carry a file");
      }

      // The download note must be hidden when real sharing is available.
      if (await isVisible(page, "#share-note")) {
        failures.push("download fallback note is shown even though the browser can share files");
      }

      await page.click("#share-submit-btn");
      await page.waitForFunction(() => (window.__shareCalls || []).length > 0, null, { timeout: 5000 })
        .catch(() => {});
      const calls = await page.evaluate(() => window.__shareCalls || []);
      const downloads = await page.evaluate(() => window.__downloads || []);
      if (!calls.length) {
        failures.push("pressing Share did not call navigator.share");
      } else {
        const f = calls[0].files[0];
        if (!f) {
          failures.push(`navigator.share was called without a file: ${JSON.stringify(calls[0])}`);
        } else if (!f.size) {
          failures.push("the shared file is empty");
        }
      }
      if (downloads.length) {
        failures.push(`fell back to a download even though sharing succeeded: ${JSON.stringify(downloads)}`);
      }
      // The dialog closes once the artifact has been handed off.
      const stillOpen = await page.evaluate(() => document.getElementById("share-dialog")?.open === true);
      if (stillOpen) failures.push("share dialog stayed open after sharing");

      if (errors.length) failures.push("JS errors (share branch): " + JSON.stringify(errors));
    } finally {
      await browser.close();
      server.close();
    }
  }

  // ---- Branch 2: the browser CANNOT share files (typical desktop).
  {
    const { server, baseUrl } = await startStubServer(stubs());
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      await installShareSpy(page, false);
      await page.reload({ waitUntil: "domcontentloaded" });
      await openCardAndDialog(page);

      // Told BEFORE pressing Share, not surprised after.
      if (!(await isVisible(page, "#share-note"))) {
        failures.push("no download-fallback note on a browser that cannot share files");
      }

      await page.click("#share-submit-btn");
      await page.waitForFunction(() => (window.__downloads || []).length > 0, null, { timeout: 5000 })
        .catch(() => {});
      const downloads = await page.evaluate(() => window.__downloads || []);
      const calls = await page.evaluate(() => window.__shareCalls || []);
      if (!downloads.length) {
        failures.push("no download fallback where file sharing is unsupported");
      }
      if (calls.length) {
        failures.push("navigator.share was called even though canShare({files}) said no");
      }

      if (errors.length) failures.push("JS errors (fallback branch): " + JSON.stringify(errors));
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}

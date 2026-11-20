// A recording that is still being processed, or that was left without a
// finished result, says so in the Library (vts-lzad). A complete one stays
// unmarked: the label is the exception, not a badge on every row.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "library-recording-state";

const IDS = {
  complete: "dddddddd-0000-0000-0000-000000000001",
  processing: "dddddddd-0000-0000-0000-000000000002",
  incomplete: "dddddddd-0000-0000-0000-000000000003",
};

function recording(id, state) {
  return {
    id,
    source_task_id: "eeeeeeee-0000-0000-0000-" + id.slice(-12),
    title: "Rec " + state,
    title_is_custom: false,
    source_url: "file://" + state + ".m4a",
    duration_sec: 630,
    language: "ru",
    tags: [],
    has_transcript: true,
    has_redacted: false,
    has_summary: true,
    has_media: false,
    prompt_results: [],
    state,
    recorded_at: "2026-08-20T10:00:00Z",
    created_at: "2026-08-20T10:00:00Z",
    updated_at: "2026-08-20T11:00:00Z",
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
    "/api/recordings": {
      items: Object.entries(IDS).map(([state, id]) => recording(id, state)),
      total: 3,
    },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    await page.waitForSelector(`#library-list [data-recording-id="${IDS.incomplete}"]`, {
      timeout: 8000,
    });
    const meta = await page.evaluate((ids) => {
      const out = {};
      for (const [state, id] of Object.entries(ids)) {
        const el = document.querySelector(`#library-list [data-recording-id="${id}"] .library-meta`);
        out[state] = el ? el.textContent.trim() : null;
      }
      return out;
    }, IDS);

    // Setup guard: an empty meta line would make the "contains neither" check vacuous.
    if (!meta.complete) failures.push(`complete: the meta line is empty or missing (${JSON.stringify(meta.complete)})`);
    else {
      for (const word of ["processing", "incomplete"]) {
        if (meta.complete.includes(word)) {
          failures.push(`complete: meta ${JSON.stringify(meta.complete)} carries "${word}"`);
        }
      }
    }
    if (!(meta.processing || "").includes("processing")) {
      failures.push(`processing: meta ${JSON.stringify(meta.processing)} does not say "processing"`);
    }
    if ((meta.processing || "").includes("incomplete")) {
      failures.push(`processing: meta ${JSON.stringify(meta.processing)} says "incomplete"`);
    }
    if (!(meta.incomplete || "").includes("incomplete")) {
      failures.push(`incomplete: meta ${JSON.stringify(meta.incomplete)} does not say "incomplete"`);
    }
    if ((meta.incomplete || "").includes("processing")) {
      failures.push(`incomplete: meta ${JSON.stringify(meta.incomplete)} says "processing"`);
    }
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

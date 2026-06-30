// Verifies the About-task dialog: hidden (and display:none) on boot — anti-flicker
// regression; opens from the stats chip; shows run parameters (language + prompt
// names); shows the results section with per-prompt finalize timings for a
// completed task; closes via the X button.
import { startStubServer, launch, openPage, isVisible, clickReal, dialogOpen } from "../harness.mjs";

export const name = "task-about-dialog";

const TASK = {
  id: "44444444-4444-4444-4444-444444444444",
  source_url: "http://x/v", source_title: "About me",
  status: "completed", summary_path: "/x/summary/final.md", transcript_path: "/x/t.txt",
  options: {
    language: "russian", audio_only: false, transcript: true,
    prompts: [{ source: "system", id: "summary" }, { source: "user", id: "u1" }],
    prompt_results: [
      { source: "system", id: "summary", name: "Summary", path: "/x/final.md", status: "completed" },
      { source: "user", id: "u1", name: "My memo", path: "/x/results/user__u1.md", status: "completed" },
    ],
  },
  steps: [
    { name: "summarize_final", status: "completed", started_at: "2026-06-28T10:01:00Z", finished_at: "2026-06-28T10:02:00Z" },
    { name: "finalize:user:u1", status: "completed", started_at: "2026-06-28T10:02:00Z", finished_at: "2026-06-28T10:03:30Z" },
  ],
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:03:30Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 2, total: 2 } },
  stats: { processing_seconds: 210, transcript_chars: 1000, redacted_chars: 800, summary_chars: 300,
           media_seconds: 600, media_bytes: 1048576 },
};

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // ANTI-FLICKER: closed dialog must be display:none right after boot.
    const closedDisplay = await page.evaluate(() => {
      const d = document.getElementById("task-about-dialog");
      return d ? getComputedStyle(d).display : "MISSING";
    });
    if (closedDisplay !== "none") {
      failures.push(`closed About-dialog display is ${JSON.stringify(closedDisplay)}, expected "none"`);
    }
    if (await isVisible(page, "#task-about-dialog")) {
      failures.push("closed About-dialog is visible on boot");
    }

    // The chip must be present and visible (task has media metrics).
    if (!(await isVisible(page, ".task-stats-chip"))) {
      failures.push(".task-stats-chip not visible (should show with media metrics)");
      return failures;
    }

    // Click the chip -> dialog opens.
    await clickReal(page, ".task-stats-chip");
    await page.waitForTimeout(200);
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About-dialog did not open from the chip");
      return failures;
    }

    // Params section: language + both prompt names.
    const info = await page.evaluate(() => {
      const q = (s) => document.querySelector(s)?.textContent || "";
      return {
        language: q(".about-language"),
        prompts: q(".about-prompts"),
        resultsHidden: document.querySelector(".about-results-section")?.classList.contains("hidden"),
        timingRows: [...document.querySelectorAll(".about-prompt-timings tr")].map(
          (tr) => [...tr.children].map((td) => td.textContent)
        ),
      };
    });
    if (info.language !== "russian") failures.push(`language wrong: ${JSON.stringify(info.language)}`);
    if (!info.prompts.includes("My memo")) failures.push(`prompts missing user name: ${JSON.stringify(info.prompts)}`);
    if (info.resultsHidden) failures.push("results section hidden for a completed task");
    if (info.timingRows.length !== 2) {
      failures.push(`expected 2 timing rows, got ${info.timingRows.length}: ${JSON.stringify(info.timingRows)}`);
    } else {
      // user:u1 finalize ran 10:02:00 -> 10:03:30 = 1:30
      const userRow = info.timingRows.find((r) => r[0] === "My memo");
      if (!userRow) failures.push(`no timing row for "My memo": ${JSON.stringify(info.timingRows)}`);
      else if (userRow[1] !== "01:30") failures.push(`user timing wrong: ${JSON.stringify(userRow)}`);
    }

    // Close via X.
    await clickReal(page, "#task-about-close-btn");
    await page.waitForTimeout(150);
    if (await dialogOpen(page, "task-about-dialog")) failures.push("About-dialog did not close via X");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

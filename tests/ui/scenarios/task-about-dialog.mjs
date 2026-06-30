// Verifies the About-task dialog: hidden (and display:none) on boot — anti-flicker
// regression; opens from the stats chip; shows run parameters (language, yes/no
// boolean ICONS, prompt names); a clickable title link; the results section with
// per-prompt finalize timings for a completed task; closes via the X button.
// Also verifies a RUNNING task with no prompt_results resolves the user prompt
// NAME from /api/prompts (not its GUID).
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

// A still-running task: has the user prompt ref but NO prompt_results yet. The
// dialog must resolve the user name from /api/prompts, not show the GUID.
const RUNNING_TASK = {
  id: "55555555-5555-5555-5555-555555555555",
  source_url: "http://x/r", source_title: "Running one",
  status: "running",
  options: {
    language: null, audio_only: true, transcript: true,
    prompts: [{ source: "system", id: "summary" }, { source: "user", id: "u1" }],
  },
  steps: [{ name: "download", status: "completed", started_at: "2026-06-28T10:00:00Z", finished_at: "2026-06-28T10:00:30Z" }],
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:00:30Z",
  progress: { transcribe: { current: 0, total: 1 }, summary: { current: 0, total: 2 } },
  stats: { media_seconds: 600, media_bytes: 1048576 },
};

const PROMPTS = [{ source: "user", id: "u1", name: "My memo", editable: true }];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK, RUNNING_TASK],
    "/api/prompts": PROMPTS,
  });
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

    // Chips: one per task card, both visible (both have media metrics).
    const chipCount = await page.evaluate(() => document.querySelectorAll(".task-stats-chip").length);
    if (chipCount < 2) {
      failures.push(`expected 2 stats chips, got ${chipCount}`);
      return failures;
    }
    // Step label for the completed task: the last step is finalize:user:u1.
    // It must render the resolved prompt name, NOT the raw "finalize:user:<uuid>".
    const stepLabel = await page.evaluate(() =>
      document.querySelector(".task:nth-of-type(1) .step-label")?.textContent || ""
    );
    if (stepLabel.includes("finalize:user:")) {
      failures.push(`step label shows raw finalize name: ${JSON.stringify(stepLabel)}`);
    }
    if (!stepLabel.includes("My memo")) {
      failures.push(`step label missing resolved prompt name: ${JSON.stringify(stepLabel)}`);
    }

    // The chip must not stretch the full card width (it sits in a grid column).
    const chipStretched = await page.evaluate(() => {
      const chip = document.querySelector(".task-stats-chip");
      const parent = chip.parentElement;
      return chip.getBoundingClientRect().width >= parent.getBoundingClientRect().width - 1;
    });
    if (chipStretched) failures.push("stats chip stretches full card width (should be content-sized)");

    // --- Completed task (first card) ---
    await clickReal(page, ".task:nth-of-type(1) .task-stats-chip");
    await page.waitForTimeout(250);
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About-dialog did not open from the chip");
      return failures;
    }

    const info = await page.evaluate(() => {
      const q = (s) => document.querySelector(s)?.textContent || "";
      const titleEl = document.querySelector(".about-source-title");
      const audioEl = document.querySelector(".about-audio-only");
      const transEl = document.querySelector(".about-transcript");
      return {
        language: q(".about-language"),
        prompts: q(".about-prompts"),
        titleText: titleEl?.textContent || "",
        titleHref: titleEl?.getAttribute("href") || "",
        titleTag: titleEl?.tagName || "",
        audioIsBool: audioEl?.classList.contains("about-bool") && audioEl.classList.contains("is-no"),
        audioHasSvg: !!audioEl?.querySelector("svg"),
        audioAria: audioEl?.getAttribute("aria-label") || "",
        transIsYes: transEl?.classList.contains("about-bool") && transEl.classList.contains("is-yes"),
        transHasSvg: !!transEl?.querySelector("svg"),
        resultsHidden: document.querySelector(".about-results-section")?.classList.contains("hidden"),
        timingRows: [...document.querySelectorAll(".about-prompt-timings tr")].map(
          (tr) => [...tr.children].map((td) => td.textContent)
        ),
      };
    });
    if (info.language !== "russian") failures.push(`language wrong: ${JSON.stringify(info.language)}`);
    if (!info.prompts.includes("My memo")) failures.push(`prompts missing user name: ${JSON.stringify(info.prompts)}`);
    // Title link: <a> with href to the source URL.
    if (info.titleTag !== "A") failures.push(`title is not an <a> link (got ${info.titleTag})`);
    if (info.titleText !== "About me") failures.push(`title text wrong: ${JSON.stringify(info.titleText)}`);
    if (info.titleHref !== "http://x/v") failures.push(`title href wrong: ${JSON.stringify(info.titleHref)}`);
    // Boolean icons: audio_only=false -> is-no + svg + aria; transcript=true -> is-yes + svg.
    if (!info.audioIsBool) failures.push("audio_only not rendered as is-no boolean icon");
    if (!info.audioHasSvg) failures.push("audio_only boolean has no svg icon");
    if (!info.audioAria) failures.push("audio_only boolean missing aria-label");
    if (!info.transIsYes) failures.push("transcript not rendered as is-yes boolean icon");
    if (!info.transHasSvg) failures.push("transcript boolean has no svg icon");
    if (info.resultsHidden) failures.push("results section hidden for a completed task");
    if (info.timingRows.length !== 2) {
      failures.push(`expected 2 timing rows, got ${info.timingRows.length}: ${JSON.stringify(info.timingRows)}`);
    } else {
      const userRow = info.timingRows.find((r) => r[0] === "My memo");
      if (!userRow) failures.push(`no timing row for "My memo": ${JSON.stringify(info.timingRows)}`);
      else if (userRow[1] !== "01:30") failures.push(`user timing wrong: ${JSON.stringify(userRow)}`);
    }

    await clickReal(page, "#task-about-close-btn");
    await page.waitForTimeout(150);
    if (await dialogOpen(page, "task-about-dialog")) failures.push("About-dialog did not close via X");

    // --- Running task (second card): name resolved from /api/prompts, not GUID ---
    await clickReal(page, ".task:nth-of-type(2) .task-stats-chip");
    await page.waitForTimeout(250);
    const running = await page.evaluate(() => {
      const q = (s) => document.querySelector(s)?.textContent || "";
      return {
        prompts: q(".about-prompts"),
        resultsHidden: document.querySelector(".about-results-section")?.classList.contains("hidden"),
        audioIsYes: document.querySelector(".about-audio-only")?.classList.contains("is-yes"),
      };
    });
    if (running.prompts.includes("u1")) failures.push(`running task shows prompt GUID instead of name: ${JSON.stringify(running.prompts)}`);
    if (!running.prompts.includes("My memo")) failures.push(`running task missing resolved user name: ${JSON.stringify(running.prompts)}`);
    if (!running.resultsHidden) failures.push("results section shown for a running (non-completed) task");
    if (!running.audioIsYes) failures.push("running task audio_only=true not rendered as is-yes");

    await clickReal(page, "#task-about-close-btn");
    await page.waitForTimeout(150);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

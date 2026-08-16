// Diana's feedback (vts-z6c8): user prompt results used to hide behind a
// dropdown inside the "Summary" tab, so nothing on a fresh task hinted that it
// would produce anything besides a summary. Each user prompt now gets its own
// tab, present from creation and disabled until its text exists.
//
// The invariants worth pinning, each of which broke something real while this
// was written:
//
//  - "Log" stays LAST (Victor's requirement). Tabs are inserted before it, not
//    appended, so a prompt tab must never land to its right.
//  - Tab ORDER follows the user's prompt selection, not prompt_results —
//    which is append-ordered by completion. Driving the strip off the results
//    would make tabs pop in one at a time and reorder mid-run.
//  - A pending prompt tab is disabled but PRESENT: that is the whole point of
//    the change ("видно сразу, с момента создания").
//  - system:summary keeps its own "Summary" tab and must NOT also appear as a
//    prompt tab — it lives in prompt_results next to the user prompts, so the
//    naive pass renders it twice.
//  - The old dropdown is gone, not merely hidden.
import { startStubServer, launch, isVisible, clickReal, screenshot } from "../harness.mjs";

export const name = "prompt-result-tabs";

const TASK_ID = "33333333-3333-3333-3333-333333333333";

// Two user prompts, deliberately listed in an order DIFFERENT from the order
// their results complete in, so a strip built from prompt_results would show
// them the other way round and fail the ordering assertion below.
const PROMPTS = [
  { source: "system", id: "summary" },
  { source: "user", id: "u-protocol" },
  { source: "user", id: "u-actions" },
];

// u-actions completed first; u-protocol is still pending.
const PROMPT_RESULTS = [
  { source: "user", id: "u-actions", name: "Поручения", path: "/x/a.md", status: "completed" },
  { source: "system", id: "summary", name: "Summary", path: "/x/s.md", status: "completed" },
];

const TASK = {
  id: TASK_ID,
  source_url: "http://x/v",
  source_title: "Prompt tabs probe",
  status: "completed",
  summary_path: "/x/summary/final.md",
  transcript_path: "/x/transcript.txt",
  options: { prompts: PROMPTS, prompt_results: PROMPT_RESULTS },
  steps: [
    { name: "transcribe", status: "completed", started_at: "2026-08-01T10:00:00Z", finished_at: "2026-08-01T10:01:00Z" },
    { name: "summarize_final", status: "completed", started_at: "2026-08-01T10:01:00Z", finished_at: "2026-08-01T10:02:00Z" },
    { name: "finalize:user:u-actions", status: "completed", started_at: "2026-08-01T10:02:00Z", finished_at: "2026-08-01T10:03:00Z" },
    { name: "finalize:user:u-protocol", status: "running", started_at: "2026-08-01T10:03:00Z" },
  ],
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:03:00Z",
  progress: {},
  stats: {},
};

const API = {
  "/api/tasks": [TASK],
  "/api/prompts": [
    { source: "system", id: "summary", name: "Summary", editable: false },
    { source: "user", id: "u-protocol", name: "Протокол", editable: true },
    { source: "user", id: "u-actions", name: "Поручения", editable: true },
  ],
  [`/api/tasks/${TASK_ID}/results/user/u-actions`]: "ACTIONS RESULT BODY",
  [`/api/tasks/${TASK_ID}/results/system/summary`]: "SUMMARY RESULT BODY",
  [`/api/tasks/${TASK_ID}/transcript`]: "TRANSCRIPT BODY",
};

// Ordered [{name, tab, disabled}] for the strip of the first task card.
const readStrip = (page) =>
  page.evaluate(() =>
    [...document.querySelectorAll(".task .tabs .tab-btn")].map((b) => ({
      tab: String(b.dataset.tab || ""),
      label: (b.textContent || "").trim(),
      disabled: b.disabled === true,
    })),
  );

export async function run() {
  const { server, baseUrl } = await startStubServer(API);
  const browser = await launch();
  const failures = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 }, locale: "ru-RU" });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);

    // The card renders collapsed; the tabs live in the body.
    await clickReal(page, ".task .toggle-btn");
    await page.waitForTimeout(400);

    const strip = await readStrip(page);
    if (!strip.length) {
      failures.push("no tab buttons found — card did not expand?");
      return failures.concat(errors);
    }

    // The old dropdown must be gone from the DOM, not just hidden.
    const dropdownGone = await page.evaluate(
      () => document.querySelectorAll(".result-prompt-bar, .result-prompt-select").length === 0,
    );
    if (!dropdownGone) {
      failures.push("the old .result-prompt-bar dropdown is still in the DOM");
    }

    // Log stays last.
    const last = strip[strip.length - 1];
    if (last.tab !== "log") {
      failures.push(
        `"Log" must remain the last tab, got "${last.tab}" (strip: ${strip.map((s) => s.tab).join(" → ")})`,
      );
    }

    // Both user prompts are present as tabs.
    const promptTabs = strip.filter((s) => s.tab.startsWith("prompt_"));
    if (promptTabs.length !== 2) {
      failures.push(
        `expected 2 user-prompt tabs, got ${promptTabs.length} (${promptTabs.map((s) => s.tab).join(", ")})`,
      );
    }

    // ...labelled with the prompt names, not ids.
    const labels = promptTabs.map((s) => s.label);
    for (const want of ["Протокол", "Поручения"]) {
      if (!labels.includes(want)) {
        failures.push(`no prompt tab labelled "${want}" (got: ${labels.join(", ") || "none"})`);
      }
    }

    // ...in SELECTION order (Протокол before Поручения), not completion order
    // (Поручения completed first, and is the only one in prompt_results).
    const iProtocol = labels.indexOf("Протокол");
    const iActions = labels.indexOf("Поручения");
    if (iProtocol >= 0 && iActions >= 0 && iProtocol > iActions) {
      failures.push(
        `prompt tabs are in completion order, not selection order: ${labels.join(" → ")}`,
      );
    }

    // system:summary must not be duplicated as a prompt tab.
    if (labels.some((l) => l === "Summary" || l === "Сводка")) {
      failures.push(`system:summary is duplicated as a prompt tab (labels: ${labels.join(", ")})`);
    }
    if (strip.filter((s) => s.tab === "summary").length !== 1) {
      failures.push(`expected exactly one built-in "summary" tab`);
    }

    // Pending prompt = present but disabled; completed prompt = enabled.
    const byLabel = Object.fromEntries(promptTabs.map((s) => [s.label, s]));
    if (byLabel["Протокол"] && !byLabel["Протокол"].disabled) {
      failures.push(`"Протокол" has no result yet and must be disabled`);
    }
    if (byLabel["Поручения"] && byLabel["Поручения"].disabled) {
      failures.push(`"Поручения" is completed and must be enabled`);
    }

    // Clicking the ready prompt tab loads ITS result (not the summary).
    const actionsTab = promptTabs.find((s) => s.label === "Поручения");
    if (actionsTab) {
      await clickReal(page, `.task .tab-btn[data-tab="${actionsTab.tab}"]`);
      await page.waitForTimeout(500);
      const shown = await page.evaluate((tab) => {
        const panel = document.querySelector(`.task .tab-content.${tab}`);
        return {
          active: panel ? panel.classList.contains("active") : false,
          text: panel ? (panel.textContent || "").trim() : null,
        };
      }, actionsTab.tab);
      if (!shown.active) {
        failures.push(`clicking "Поручения" did not activate its panel`);
      }
      if (shown.text !== "ACTIONS RESULT BODY") {
        failures.push(
          `"Поручения" panel shows ${JSON.stringify(shown.text)}, expected its own result body`,
        );
      }
    }

    // A disabled tab must not activate on click.
    const protocolTab = promptTabs.find((s) => s.label === "Протокол");
    if (protocolTab) {
      const before = await page.evaluate(
        () => document.querySelector(".task .tab-btn.active")?.dataset.tab || "",
      );
      await page.evaluate((tab) => {
        document.querySelector(`.task .tab-btn[data-tab="${tab}"]`)?.click();
      }, protocolTab.tab);
      await page.waitForTimeout(300);
      const after = await page.evaluate(
        () => document.querySelector(".task .tab-btn.active")?.dataset.tab || "",
      );
      if (after === protocolTab.tab || after !== before) {
        failures.push(`clicking the disabled "Протокол" tab changed the active tab to "${after}"`);
      }
    }

    // The built-in Summary tab still renders system:summary.
    await clickReal(page, '.task .tab-btn[data-tab="summary"]');
    await page.waitForTimeout(500);
    const summaryText = await page.evaluate(
      () => (document.querySelector(".task .tab-content.summary")?.textContent || "").trim(),
    );
    if (summaryText !== "SUMMARY RESULT BODY") {
      failures.push(`Summary tab shows ${JSON.stringify(summaryText)}, expected the summary result`);
    }

    await screenshot(page, "prompt-result-tabs");
    failures.push(...errors);
    await page.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

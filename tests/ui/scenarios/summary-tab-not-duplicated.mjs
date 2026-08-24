// Regression: the vendor prompt got a SECOND tab of its own, next to the
// built-in "Сводка" — two tabs for one object.
//
// syncPromptTabs() builds one tab per prompt and deliberately skips the vendor
// one, "because it already has its own Summary tab". That skip tested
// ref.source === "user", which was correct while the vendor prompt was
// {source:"system", id:"summary"}. Since vts-kujy it is an ordinary row with a
// generated uuid, served as source:"user" with is_system set — so the filter no
// longer excluded it and it got a duplicate tab.
//
// The two halves of the bug show up differently, and both are asserted here:
//   - the duplicate tab renders the result (it reads prompt_results), while
//   - the built-in Summary tab asked for {source:"system", id:"summary"},
//     found nothing in prompt_results, fell back to the legacy /summary
//     endpoint and showed `404: Summary is not ready`.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "summary-tab-not-duplicated";

const TASK_ID = "99999999-9999-9999-9999-999999999999";
// The vendor prompt as it exists today: a real row, source "user", is_system.
const SYSTEM_PROMPT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const USER_PROMPT_ID = "u1";
const SUMMARY_TEXT = "# Themes\n\nCompetitor research strategy...";

const TASK = {
  id: TASK_ID, source_url: "file://meeting.webm", source_title: "Meeting recording",
  status: "completed", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: "/t.txt", summary_path: "/s.md", media_path: null,
  options: {
    transcript: true, diarize: true,
    prompts: [
      { source: "user", id: SYSTEM_PROMPT_ID },
      { source: "user", id: USER_PROMPT_ID },
    ],
    prompt_results: [
      { source: "user", id: SYSTEM_PROMPT_ID, name: "Summary", status: "completed" },
      { source: "user", id: USER_PROMPT_ID, name: "Memo", status: "completed" },
    ],
  },
  steps: [],
  capabilities: { can_restart_summary: true, can_restart_final_summary: false },
  created_at: "2026-08-24T10:00:00Z", updated_at: "2026-08-24T11:00:00Z",
  progress: {}, stats: {},
};

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK],
    "/api/prompts": [
      // is_system marks the vendor prompt even though source is "user".
      { source: "user", id: SYSTEM_PROMPT_ID, name: "Summary", editable: true, is_system: true },
      { source: "user", id: USER_PROMPT_ID, name: "Memo", editable: true, is_system: false },
    ],
    [`/api/tasks/${TASK_ID}/results/user/${SYSTEM_PROMPT_ID}`]: SUMMARY_TEXT,
    [`/api/tasks/${TASK_ID}/results/user/${USER_PROMPT_ID}`]: "memo body",
    // The legacy endpoint the built-in tab used to fall back to. The real
    // server 404s here for a task whose vendor prompt is the modern row, and
    // api() surfaces that as the string `404: {"detail":"Summary is not
    // ready"}` — the exact text on the user's screenshot. The stub always
    // answers 200, so encode that rendered form as the body: what matters is
    // that the built-in tab must not be reading this endpoint at all.
    [`/api/tasks/${TASK_ID}/summary`]: '404: {"detail":"Summary is not ready"}',
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${TASK_ID}"]`, { timeout: 5000 });

    // Expand the card so the tab strip is built.
    await page.click(`[data-task-id="${TASK_ID}"] .task-right-top`);
    await page.waitForTimeout(500);

    const tabs = await page.evaluate((i) => {
      const btns = [...document.querySelectorAll(`[data-task-id="${i}"] .tab-btn`)];
      return btns.map((b) => ({
        tab: b.dataset.tab || "",
        text: (b.textContent || "").trim(),
        promptId: b.dataset.promptId || "",
        disabled: b.disabled === true,
      }));
    }, TASK_ID);

    // 1. The vendor prompt must NOT get a tab of its own — the built-in
    //    "summary" tab already represents it.
    const dupes = tabs.filter((t) => t.promptId === SYSTEM_PROMPT_ID);
    if (dupes.length) {
      failures.push(
        `the vendor (is_system) prompt got its own tab ${JSON.stringify(dupes)} ` +
        `in addition to the built-in Summary tab — two tabs for one object`
      );
    }

    // The genuine user prompt still gets one.
    if (!tabs.some((t) => t.promptId === USER_PROMPT_ID)) {
      failures.push("the real user prompt lost its tab: " + JSON.stringify(tabs));
    }
    // Exactly one summary-ish tab overall.
    const summaryTabs = tabs.filter((t) => t.tab === "summary" || t.promptId === SYSTEM_PROMPT_ID);
    if (summaryTabs.length !== 1) {
      failures.push(`expected exactly 1 summary tab, got ${summaryTabs.length}: ${JSON.stringify(summaryTabs)}`);
    }

    // 2. The built-in Summary tab must show the actual result, not the legacy
    //    404 — it has to resolve the vendor prompt the new way.
    await page.click(`[data-task-id="${TASK_ID}"] .tab-btn[data-tab="summary"]`);
    await page.waitForTimeout(500);
    const body = await page.evaluate((i) => {
      const el = document.querySelector(`[data-task-id="${i}"] .tab-content.summary`);
      return el ? (el.textContent || "").trim() : null;
    }, TASK_ID);

    if (body === null) {
      failures.push("no .tab-content.summary panel");
    } else if (/Summary is not ready|^404/.test(body)) {
      failures.push(
        `built-in Summary tab shows the legacy 404 (${JSON.stringify(body.slice(0, 60))}) — ` +
        `it still resolves the vendor prompt as {source:"system", id:"summary"}`
      );
    } else if (!body.includes("Themes")) {
      failures.push(`built-in Summary tab did not render the result, got ${JSON.stringify(body.slice(0, 80))}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

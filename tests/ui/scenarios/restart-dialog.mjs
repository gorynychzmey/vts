// Verifies: closed restart dialog is hidden (not leaking onto the page);
// it opens from the task menu; it closes via the X button. This is the exact
// regression class that shipped (display:flex overrode closed <dialog> display:none).
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot } from "../harness.mjs";

export const name = "restart-dialog";

const COMPLETED_TASK = {
  id: "11111111-1111-1111-1111-111111111111",
  source_url: "http://x/v", source_title: "Test",
  status: "completed", summary_path: "/x/summary/final.md",
  options: {
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "summarize_windows", status: "completed", started_at: "2026-06-28T10:00:00Z", finished_at: "2026-06-28T10:01:00Z" },
    { name: "summarize_final", status: "completed", started_at: "2026-06-28T10:01:00Z", finished_at: "2026-06-28T10:02:00Z" },
  ],
  // The real API always sends these (vts.api.main.can_restart_*_task); for a
  // completed task with a selected summary prompt and completed summarize_windows
  // both are true. The frontend reads them instead of re-deriving the rule (vts-c2n).
  capabilities: { can_restart_summary: true, can_restart_final_summary: true },
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:02:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 2, total: 2 } }, stats: {},
};

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [COMPLETED_TASK],
    "/api/presets": [
      { source: "system", id: "default", name: "Default", editable: false,
        options: { language: null, audio_only: false, transcript: true,
                   prompts: [{ source: "system", id: "summary" }] } },
      { source: "user", id: "p1", name: "Memo preset", editable: true,
        options: { language: null, audio_only: false, transcript: true,
                   prompts: [{ source: "user", id: "u1" }] } },
    ],
    "/api/me/default_preset": { source: "system", id: "default" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // CLOSED STATE (the critical assertion): the dialog must be hidden before any open.
    if (await isVisible(page, "#restart-final-dialog")) {
      failures.push("closed restart dialog is VISIBLE (should be display:none)");
    }

    // The restart menu button must exist on the rendered task row.
    if (!(await page.$(".restart-summary-btn"))) {
      failures.push("no .restart-summary-btn on the rendered task row");
    } else {
      // Open the menu, then click "Restart final summary only".
      await clickReal(page, ".restart-summary-btn");
      await page.waitForTimeout(150);
      const finalBtn = await page.$(".restart-summary-final-btn");
      if (!finalBtn) {
        failures.push("no .restart-summary-final-btn in menu");
      } else if (await finalBtn.isDisabled()) {
        failures.push(".restart-summary-final-btn is disabled (gate)");
      } else {
        await finalBtn.click();
        await page.waitForTimeout(300);

        if (!(await dialogOpen(page, "restart-final-dialog"))) {
          failures.push("restart dialog did not open from the menu");
        } else {
          await screenshot(page, "restart-dialog-open");

          // Preset dropdown present and neutral by default
          const presetVal = await page.evaluate(() => {
            const el = document.getElementById("restart-final-preset");
            return el ? el.value : "__missing__";
          });
          if (presetVal !== "") failures.push(`restart preset dropdown not neutral on open (got ${JSON.stringify(presetVal)})`);

          // Selecting the user preset applies its prompts to the multiselect
          const applied = await page.evaluate(() => {
            const el = document.getElementById("restart-final-preset");
            el.value = "user:p1";
            el.dispatchEvent(new Event("change", { bubbles: true }));
            const checked = [...document.querySelectorAll('#restart-final-select input[type="checkbox"]:checked')]
              .map((c) => `${c.dataset.source}:${c.dataset.id}`);
            return checked;
          });
          if (!(applied.length === 1 && applied[0] === "user:u1")) {
            failures.push(`preset apply did not set prompts to [user:u1] (got ${JSON.stringify(applied)})`);
          }

          // CLOSE via the X button — must actually close.
          await clickReal(page, "#restart-final-close-btn");
          await page.waitForTimeout(200);
          if (await dialogOpen(page, "restart-final-dialog")) {
            failures.push("restart dialog did NOT close via the X button");
          }
          // And after closing, it must be hidden again.
          if (await isVisible(page, "#restart-final-dialog")) {
            failures.push("restart dialog visible after close");
          }
        }
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

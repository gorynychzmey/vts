// A tooltip must not outlive the pointer (vts-tip1).
//
// Reported: hover a button until its tooltip appears, click it, move away —
// the tooltip stayed up until you clicked somewhere empty. A mouse click
// focuses the button, and the reveal rule listed plain `:focus`, so focus kept
// showing what hover had started.
//
// `:focus` was there on purpose: a touch tap has no hover, and focus was the
// only way to reveal a tooltip on a phone. So the fix is `:focus-visible`,
// which browsers set for keyboard focus and for taps on controls without a
// hover state — the touch case keeps working while the mouse case does not
// stick. That distinction is what this scenario guards.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "tooltip-not-sticky";

const TASK_ID = "44444444-4444-4444-4444-444444444444";
const TASK = {
  id: TASK_ID, source_url: "file://a.webm", source_title: "A task",
  status: "completed", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: "/t.txt", summary_path: "/s.md", media_path: null,
  options: { transcript: true, prompts: [], prompt_results: [] },
  steps: [], capabilities: {},
  created_at: "2026-08-20T10:00:00Z", updated_at: "2026-08-20T11:00:00Z",
  progress: {}, stats: {},
};

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    const SEL = `[data-task-id="${TASK_ID}"]`;
    await page.waitForSelector(SEL, { timeout: 8000 });
    await page.click(`${SEL} .task-right-top`);
    await page.waitForSelector(`${SEL} .tab-content.transcript.active`, { timeout: 8000 });

    const btn = `${SEL} .task-menu-btn`;
    const tooltipState = () => page.evaluate((s) => {
      const el = document.querySelector(s);
      if (!el) return null;
      const cs = getComputedStyle(el, "::after");
      return { visible: cs.visibility === "visible" && cs.opacity !== "0",
               focused: document.activeElement === el };
    }, btn);

    // Hover past the 0.5s dwell: the tooltip should appear.
    await page.hover(btn);
    await page.waitForTimeout(900);
    const hovered = await tooltipState();
    if (!hovered || !hovered.visible) {
      failures.push("the tooltip never appeared on hover — the reveal is broken");
      return failures;
    }

    await page.click(btn);
    await page.waitForTimeout(200);

    // Pointer away. Focus is still on the button; the tooltip must not be.
    await page.mouse.move(5, 5);
    await page.waitForTimeout(700);
    const after = await tooltipState();
    if (after.visible) {
      failures.push(
        "the tooltip is still shown after the pointer left — it is being held " +
        "by focus, and only a click elsewhere dismisses it"
      );
    }
    if (!after.focused) {
      // Not the bug, but worth knowing: the test would pass for the wrong
      // reason if the click had not focused the button at all.
      failures.push("the button lost focus, so this run did not test the bug");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

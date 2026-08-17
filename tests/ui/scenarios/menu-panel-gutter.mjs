// The first row of a dropdown sat flush against the panel's top border: the
// panel (.btn-menu) had no padding at all, while its rows carry a border-radius
// and a hover background. So the top row's label touched the edge, and its
// hover box collided with the panel's own rounded corner. Reported by Victor
// from the shipped build against the task kebab; the header burger and the
// restart sub-panel share the same container and had it too.
//
// Measured, not asserted on the CSS property: what was wrong is the visible
// distance between the panel's top edge and the first row's box.
import { startStubServer, launch, openPage, clickReal } from "../harness.mjs";

export const name = "menu-panel-gutter";

// A gutter has to be visible, but must not read as a stray blank band.
const MIN = 3;
const MAX = 12;

const iso = new Date().toISOString();
const TASKS = [{
  id: "t0", status: "completed",
  source_url: "https://y/t0", source_title: "Meeting recording",
  display_name: "Meeting recording",
  created_at: iso, updated_at: iso, media_path: "/m.mp4",
  summary_path: "/x/summary/final.md",
  options: {
    transcript: true,
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "summarize_windows", status: "completed", started_at: iso, finished_at: iso },
    { name: "summarize_final", status: "completed", started_at: iso, finished_at: iso },
  ],
  // Enables the restart row, which is the gateway to the sub-panel measured
  // below; the frontend reads these instead of re-deriving the rule (vts-c2n).
  capabilities: { can_restart_summary: true, can_restart_final_summary: true },
}];

// Distance from the panel's padding-box top to the topmost visible row, plus
// the symmetric measure at the bottom — a panel padded only at the top would
// look just as wrong the other way.
function measure(page, panelSel) {
  return page.evaluate((sel) => {
    const panel = document.querySelector(sel);
    if (!panel) return null;
    const pr = panel.getBoundingClientRect();
    const rows = [...panel.children].filter(
      (el) => el.getBoundingClientRect().height > 0
    );
    if (!rows.length) return { empty: true };
    const first = rows[0].getBoundingClientRect();
    const last = rows[rows.length - 1].getBoundingClientRect();
    return {
      top: Math.round(first.top - pr.top),
      bottom: Math.round(pr.bottom - last.bottom),
      // The row must also not touch the side borders.
      left: Math.round(first.left - pr.left),
    };
  }, panelSel);
}

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const browser = await launch();
  const failures = [];

  const check = (label, m) => {
    if (!m) return failures.push(`${label}: panel not found`);
    if (m.empty) return failures.push(`${label}: panel has no visible rows`);
    for (const side of ["top", "bottom", "left"]) {
      const v = m[side];
      if (v < MIN) {
        failures.push(
          `${label}: first/last row is flush against the ${side} border ` +
          `(${v}px, want >=${MIN}px) — the panel needs its own gutter`
        );
      } else if (v > MAX) {
        failures.push(`${label}: ${side} gutter is ${v}px, want <=${MAX}px`);
      }
    }
  };

  try {
    const { page } = await openPage(browser, baseUrl, { width: 1100, height: 800 });

    // Task kebab — the panel Victor reported.
    await clickReal(page, ".task .task-menu-btn");
    await page.waitForTimeout(250);
    check("task menu", await measure(page, ".task-menu.open"));

    // The restart sub-panel is a second .btn-menu, opened from a row of the first.
    await clickReal(page, ".task-menu.open .restart-summary-btn");
    await page.waitForTimeout(250);
    check("restart submenu", await measure(page, ".restart-summary-menu.open"));

    // Header burger: same container, but its rows are bare <button>s that never
    // got .menu-item, so they are laid out by a different rule.
    await page.evaluate(() => window.scrollTo(0, 0));
    await clickReal(page, "#header-menu-btn");
    await page.waitForTimeout(250);
    check("header menu", await measure(page, "#header-menu.open"));

    await page.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

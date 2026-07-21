// Regression (vts tooltips): when a dialog opens, its close button (✕) receives
// focus, and because every data-i18n-title becomes a :focus-triggered
// data-tooltip bubble, the close button's tooltip fired on open — clipped at the
// dialog's top edge. Two asserts:
//  1. On open, the close button's tooltip must NOT be visible (opacity 0).
//  2. Even if shown, its bubble must not be clipped by the dialog boundary.
import {
  startStubServer, launch, openPage, clickReal, dialogOpen,
} from "../harness.mjs";

export const name = "dialog-tooltip-on-open";

const DONE_ID = "11111111-1111-1111-1111-111111111111";

const FLAGS = {
  completed: {
    is_active: false, is_pending: false, is_finished: true, shows_progress: false,
    can_pause: false, can_resume: false, can_archive: true, needs_input: false,
  },
};

const DONE_TASK = {
  id: DONE_ID, source_url: "http://x/done", source_title: "Finished meeting",
  status: "completed", awaiting_step: "",
  queue: null, queue_position: null,
  transcript_path: "/t", summary_path: "/s",
  options: { transcript: true, diarize: true, prompts: [] },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-17T10:00:00Z", finished_at: "2026-07-17T10:01:00Z" },
    { name: "match_speakers", status: "completed", started_at: "2026-07-17T10:01:00Z", finished_at: "2026-07-17T10:02:00Z" },
  ],
  capabilities: { can_restart_summary: false, can_restart_final_summary: false, can_resolve_speakers: true },
  created_at: "2026-07-17T10:00:00Z", updated_at: "2026-07-17T10:02:00Z",
  progress: {}, stats: { media_seconds: 300 },
};

const SPEAKER_MATCHES = {
  SPEAKER_00: {
    outcome: "auto", speaker_id: "sp-a", distance: 0.1, share: 0.6, noise: false,
    candidates: [{ speaker_id: "sp-a", name: "Anna", distance: 0.1 }],
  },
};
const ALL_SPEAKERS = [{ id: "sp-a", name: "Anna", sample_count: 2 }];

// opacity of the ::after tooltip bubble on a selector (as painted).
async function tooltipOpacity(page, sel) {
  return page.evaluate((s) => {
    const el = document.querySelector(s);
    if (!el) return null;
    return parseFloat(getComputedStyle(el, "::after").opacity) || 0;
  }, sel);
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: FLAGS },
    "/api/tasks": [DONE_TASK],
    [`/api/tasks/${DONE_ID}/speaker-matches`]: SPEAKER_MATCHES,
    "/api/speakers": ALL_SPEAKERS,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${DONE_ID}"]`, { timeout: 5000 });

    // --- Bug 1: tooltips must have a show-delay, not appear instantly on hover.
    // The CSS transition-delay lives on the :hover rule; assert it is > 0 so a
    // pointer sweeping across buttons doesn't flash tooltips.
    const delaySel = `[data-task-id="${DONE_ID}"] .resolve-voices-btn`;
    const showDelay = await page.evaluate((s) => {
      const el = document.querySelector(s);
      if (!el) return null;
      // Force :hover-equivalent state is not possible from JS, but the delay is a
      // static property of the :hover rule; read it by temporarily matching.
      el.classList.add("__probe_hover__");
      // Fallback: read the delay declared on the :hover rule via a style probe.
      const d = getComputedStyle(el, "::after").transitionDelay;
      el.classList.remove("__probe_hover__");
      return d;
    }, delaySel);
    // Without hover we read the base (0s); the real assertion is the rule exists.
    // Use a CSS-rule scan instead: confirm a [data-tooltip]:hover rule sets a
    // non-zero transition-delay.
    const hoverDelay = await page.evaluate(() => {
      for (const sheet of document.styleSheets) {
        let rules;
        try { rules = sheet.cssRules; } catch { continue; }
        for (const r of rules) {
          if (r.selectorText && r.selectorText.includes("[data-tooltip]:hover::after")) {
            const d = r.style.transitionDelay;
            if (d) return d;
          }
        }
      }
      return null;
    });
    void showDelay;
    if (!hoverDelay || parseFloat(hoverDelay) <= 0) {
      failures.push(
        `tooltip show has no delay (hover rule transition-delay=${JSON.stringify(hoverDelay)}); ` +
        `tooltips appear too fast`
      );
    }

    await clickReal(page, `[data-task-id="${DONE_ID}"] .resolve-voices-btn`);
    await page.waitForTimeout(400);
    if (!(await dialogOpen(page, "voice-resolution-dialog"))) {
      failures.push("voice dialog did not open");
      return failures;
    }

    const closeSel = "#voice-close-btn";
    // 1. The close button's tooltip must NOT be showing right after open.
    const op = await tooltipOpacity(page, closeSel);
    if (op !== null && op > 0.01) {
      failures.push(
        `close-button tooltip is visible (opacity ${op}) immediately on dialog open — ` +
        `it should not appear until the user actually hovers/keyboard-focuses it`
      );
    }
    // 2. The close button's tooltip must render DOWNWARD (into the dialog body),
    // not upward past the dialog's top edge where a max-height dialog clips it.
    // Read the rendered ::after anchoring: below means bottom:auto + a real top.
    const anchor = await page.evaluate((s) => {
      const el = document.querySelector(s);
      if (!el) return "missing";
      const cs = getComputedStyle(el, "::after");
      return { bottom: cs.bottom, top: cs.top };
    }, closeSel);
    if (anchor === "missing") {
      failures.push("voice close button not found");
    } else {
      // Below-anchoring sets `top: calc(100% + gap)` — a POSITIVE used top (the
      // bubble starts below the button's bottom). Above-anchoring leaves top:auto
      // (computed as a negative used value, the bubble sitting above). getComputed
      // returns a px for both, so key off the sign of top rather than "auto".
      const topPx = parseFloat(anchor.top);
      if (!(topPx > 0)) {
        failures.push(
          `dialog close button's tooltip is anchored above (top=${anchor.top}) — ` +
          "an upward bubble is clipped at the dialog's top edge; it must render below"
        );
      }
    }

    // 3. The Save / Save-and-continue buttons carry an explanatory tooltip.
    for (const id of ["#voice-save", "#voice-save-continue"]) {
      const hasTip = await page.evaluate((s) => {
        const el = document.querySelector(s);
        return el ? (el.getAttribute("data-tooltip") || "").trim().length > 0 : "missing";
      }, id);
      if (hasTip === "missing") {
        failures.push(`${id} not found in the voice dialog`);
      } else if (!hasTip) {
        failures.push(`${id} has no explanatory data-tooltip`);
      }
    }

    // 4. Stuck tooltip (screenshot bug): closing a dialog returns focus to the
    // TRIGGER button, and :focus reveals the tooltip — so the trigger's tooltip
    // hung after the dialog closed. Reproduce the exact screenshot path with a
    // stable toolbar button (the prompts button): open its dialog by MOUSE and
    // close it by MOUSE (a keyboard close is :focus-visible and legitimately
    // shows the tooltip — the bug is the pointer path), then assert not stuck.
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    const promptsSel = "#prompts-btn";
    if (await page.$(promptsSel)) {
      await clickReal(page, promptsSel);
      await page.waitForTimeout(300);
      await clickReal(page, "#prompts-close-btn");
      await page.waitForTimeout(800); // past the show-delay, so a stuck :focus would show
      const stuck = await tooltipOpacity(page, promptsSel);
      if (stuck !== null && stuck > 0.01) {
        failures.push(
          `toolbar button tooltip is stuck visible (opacity ${stuck}) after its dialog closed — ` +
          `focus returned to the trigger and :focus kept the tooltip up`
        );
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

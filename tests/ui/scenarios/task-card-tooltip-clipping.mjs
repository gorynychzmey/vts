// Verifies two regressions reported against 1.5.2 (vts-552):
//
//  1. Tooltips on the task-card action buttons were clipped by the card. The
//     card carried `overflow: hidden` (for its rounded corners) and every
//     data-i18n-title becomes an absolutely-positioned data-tooltip bubble at
//     runtime, so the bubble was cut off at the card's edge. Geometry is the
//     only honest assertion here: opacity was already "1" while the bubble was
//     visually truncated, which is why the existing tooltip scenarios passed
//     straight through the bug.
//
//  2. The About dialog never mentioned diarization at all — "Run parameters"
//     listed language / audio-only / transcript / prompts and silently omitted
//     the Speakers flag, so there was no way to tell whether a task diarized.
import { startStubServer, launch, openPage, clickReal, dialogOpen } from "../harness.mjs";

export const name = "task-card-tooltip-clipping";

const DIARIZED_TASK = {
  id: "66666666-6666-6666-6666-666666666666",
  source_url: "http://x/d", source_title: "Diarized meeting",
  status: "completed", summary_path: "/x/summary/final.md", transcript_path: "/x/t.txt",
  options: {
    language: "russian", audio_only: false, transcript: true, diarize: true,
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [
      { source: "system", id: "summary", name: "Summary", path: "/x/final.md", status: "completed" },
    ],
  },
  steps: [
    { name: "diarize", status: "completed", started_at: "2026-07-19T10:00:00Z", finished_at: "2026-07-19T10:05:00Z" },
  ],
  created_at: "2026-07-19T10:00:00Z", updated_at: "2026-07-19T10:05:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 1, total: 1 } },
  stats: { processing_seconds: 300, transcript_chars: 1000, summary_chars: 300,
           media_seconds: 600, media_bytes: 1048576 },
};

// Same shape, diarization off: the row must read "no", not vanish.
const PLAIN_TASK = {
  ...DIARIZED_TASK,
  id: "77777777-7777-7777-7777-777777777777",
  source_title: "Plain transcript",
  options: { ...DIARIZED_TASK.options, diarize: false },
};

// Measures whether a tooltip bubble is clipped by any scroll/overflow ancestor.
// Compares the bubble's own border box against the clipping rect of every
// ancestor that establishes one; a bubble wider/taller than what actually
// paints means the user reads truncated text.
async function bubbleClip(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const style = getComputedStyle(el, "::after");
    const box = el.getBoundingClientRect();
    // The ::after box is positioned relative to the trigger; derive its rect
    // from the computed width/height plus the trigger's own position.
    const w = parseFloat(style.width) || 0;
    const h = parseFloat(style.height) || 0;
    if (!w || !h) return { measured: false };

    // Walk ancestors looking for a clipping box that cuts the bubble.
    let worstOverflow = 0;
    let clipper = "";
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ps = getComputedStyle(p);
      const clips = ["hidden", "auto", "scroll", "clip"].some(
        (v) => ps.overflowX === v || ps.overflowY === v
      );
      if (!clips) continue;
      const pr = p.getBoundingClientRect();
      // The bubble sits above the trigger, horizontally centred (or edge
      // anchored). Take its horizontal extent from the trigger's centre.
      const left = box.left + box.width / 2 - w / 2;
      const right = left + w;
      const top = box.top - h;
      const overflowLeft = Math.max(0, pr.left - left);
      const overflowRight = Math.max(0, right - pr.right);
      const overflowTop = ps.overflowY === "visible" ? 0 : Math.max(0, pr.top - top);
      const worst = Math.max(overflowLeft, overflowRight, overflowTop);
      if (worst > worstOverflow) {
        worstOverflow = worst;
        clipper = p.className || p.tagName;
      }
    }
    return { measured: true, overflow: worstOverflow, clipper, width: w };
  }, selector);
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [DIARIZED_TASK, PLAIN_TASK],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.setViewportSize({ width: 1100, height: 800 });
    await page.waitForTimeout(400);

    // ---- 1. Task card must not establish a clipping box over its tooltips ----
    const cardOverflow = await page.evaluate(() => {
      const card = document.querySelector(".task");
      if (!card) return null;
      const s = getComputedStyle(card);
      return { x: s.overflowX, y: s.overflowY, radius: s.borderTopLeftRadius };
    });
    if (!cardOverflow) {
      failures.push("no .task card rendered");
    } else {
      if (cardOverflow.x === "hidden" || cardOverflow.y === "hidden") {
        failures.push(
          `.task still clips its tooltips (overflow ${cardOverflow.x}/${cardOverflow.y})`
        );
      }
      // The rounded corner is the reason the clipping existed — it must survive.
      if (parseFloat(cardOverflow.radius) <= 0) {
        failures.push(`.task lost its rounded corner (radius "${cardOverflow.radius}")`);
      }
    }

    // The tinted header row must now round itself, or the fix trades a clipped
    // tooltip for a square corner painting over the card's border.
    const headerRadius = await page.evaluate(() => {
      const h = document.querySelector(".task-header-row");
      return h ? getComputedStyle(h).borderTopLeftRadius : null;
    });
    if (headerRadius !== null && parseFloat(headerRadius) <= 0) {
      failures.push(`.task-header-row has no top radius ("${headerRadius}") — corner will show square`);
    }

    // ---- 2. A real tooltip on a task action button must not be clipped ----
    const btnSel = ".task .task-right [data-tooltip]";
    const btn = await page.$(btnSel);
    if (!btn) {
      failures.push("no task action button with [data-tooltip] found");
    } else {
      await page.hover(btnSel);
      await page.waitForTimeout(250);
      const clip = await bubbleClip(page, btnSel);
      if (!clip || !clip.measured) {
        failures.push("could not measure tooltip bubble geometry");
      } else if (clip.overflow > 1) {
        failures.push(
          `task action tooltip clipped by .${clip.clipper}: ` +
          `${clip.overflow.toFixed(1)}px of a ${clip.width.toFixed(0)}px bubble cut off`
        );
      }
    }

    // ---- 3. About dialog exposes the diarize flag ----
    await page.mouse.move(0, 0);
    await clickReal(page, ".task .task-stats-chip");
    await page.waitForTimeout(300);
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About dialog did not open from the stats chip");
    } else {
      // Booleans render as an SVG yes/no icon (setAboutBool), so textContent is
      // legitimately empty — the contract is the is-yes/is-no class plus the
      // aria-label, which is also what a screen reader announces.
      const row = await page.evaluate(() => {
        const value = document.querySelector(".about-diarize");
        if (!value) return null;
        const label = value.closest(".about-row")?.querySelector(".about-label");
        const vs = getComputedStyle(value);
        return {
          label: (label?.textContent || "").trim(),
          yes: value.classList.contains("is-yes"),
          no: value.classList.contains("is-no"),
          aria: value.getAttribute("aria-label") || "",
          hasIcon: !!value.querySelector("svg"),
          visible: vs.display !== "none" && vs.visibility !== "hidden",
        };
      });
      if (!row) {
        failures.push("About dialog has no .about-diarize row — diarization still unreported");
      } else {
        if (!row.visible) failures.push("the diarize row is present but not visible");
        if (!row.label) failures.push("the diarize row has an empty label (missing i18n key)");
        if (!row.hasIcon) failures.push("the diarize row rendered no yes/no icon");
        if (!row.aria) failures.push("the diarize row has no aria-label");
        // This task has diarize: true, so it must read yes.
        if (!row.yes || row.no) {
          failures.push(`diarize row should read yes for a diarized task (is-yes=${row.yes}, is-no=${row.no})`);
        }
      }
    }

    // The "no" case must render too — an omitted row would be the old bug in
    // a narrower form (a task that did not diarize looking identical to one
    // whose flag was silently dropped).
    await page.click("#task-about-close-btn").catch(() => {});
    await page.waitForTimeout(200);
    const chips = await page.$$(".task .task-stats-chip");
    if (chips.length > 1) {
      await chips[1].click();
      await page.waitForTimeout(300);
      const plain = await page.evaluate(() => {
        const v = document.querySelector(".about-diarize");
        return v ? { yes: v.classList.contains("is-yes"), no: v.classList.contains("is-no") } : null;
      });
      if (!plain) {
        failures.push("diarize row missing for a non-diarized task");
      } else if (!plain.no || plain.yes) {
        failures.push(`diarize row should read no for a non-diarized task (is-yes=${plain.yes}, is-no=${plain.no})`);
      }
    }

    if (errors.length) failures.push(`console errors: ${errors.join(" | ")}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

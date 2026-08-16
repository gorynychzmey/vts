// Diana's feedback (vts-w6o6): the tab strip scrolls, but a cut-off tab just
// looks cut off — nothing tells you there is more to either side. The strip now
// fades the edge that has hidden content.
//
// Two things are pinned here:
//
//  1. The hints track the real scroll position: nothing faded when the strip
//     fits, the RIGHT edge faded when parked at the start of an overflowing
//     strip, the LEFT edge faded once scrolled to the end, and both while in
//     the middle. Asserted through the data-scroll-* flags AND the computed
//     mask, so deleting either half of the mechanism fails.
//  2. It costs no layout. The obvious implementation — an absolutely positioned
//     ::after overlay — sits inside the scrollable box and extends its
//     scrollWidth, which is exactly the invisible-horizontal-scroll trap from
//     vts-nr4. A mask paints without participating in layout, so the page must
//     still not scroll sideways at phone widths.
import { startStubServer, launch, clickReal, screenshot } from "../harness.mjs";

export const name = "tabs-scroll-affordance";

const TASK_ID = "44444444-4444-4444-4444-444444444444";

// Enough prompts with long names to overflow the strip at any width.
const NAMES = [
  "Протокол совещания",
  "Поручения и сроки",
  "Риски и блокеры",
  "Решения по бюджету",
  "Follow-up для клиента",
  "Краткие тезисы",
];
const IDS = NAMES.map((_, i) => `u-${i}`);

const TASK = {
  id: TASK_ID,
  source_url: "http://x/v",
  source_title: "Scroll affordance probe",
  status: "completed",
  summary_path: "/x/summary/final.md",
  transcript_path: "/x/transcript.txt",
  options: {
    prompts: [{ source: "system", id: "summary" }, ...IDS.map((id) => ({ source: "user", id }))],
    prompt_results: [
      { source: "system", id: "summary", name: "Summary", path: "/x/s.md", status: "completed" },
      ...IDS.map((id, i) => ({
        source: "user",
        id,
        name: NAMES[i],
        path: `/x/${id}.md`,
        status: "completed",
      })),
    ],
  },
  steps: [
    { name: "transcribe", status: "completed", started_at: "2026-08-01T10:00:00Z", finished_at: "2026-08-01T10:01:00Z" },
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
    ...IDS.map((id, i) => ({ source: "user", id, name: NAMES[i], editable: true })),
  ],
};

const readHints = (page) =>
  page.evaluate(() => {
    const bar = document.querySelector(".task .tabs");
    if (!bar) return null;
    const cs = getComputedStyle(bar);
    return {
      start: bar.dataset.scrollStart,
      end: bar.dataset.scrollEnd,
      scrollLeft: Math.round(bar.scrollLeft),
      maxScroll: Math.round(bar.scrollWidth - bar.clientWidth),
      // Either property may carry it depending on the engine.
      mask: `${cs.maskImage || "none"}|${cs.webkitMaskImage || "none"}`,
    };
  });

export async function run() {
  const { server, baseUrl } = await startStubServer(API);
  const browser = await launch();
  const failures = [];
  try {
    // --- Narrow: the strip overflows, so the hints must engage. ---
    const page = await browser.newPage({ viewport: { width: 380, height: 900 }, locale: "ru-RU" });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.waitForTimeout(500);
    await clickReal(page, ".task .toggle-btn");
    await page.waitForTimeout(500);

    const atStart = await readHints(page);
    if (!atStart) {
      failures.push("no .tabs strip found — card did not expand?");
      return failures.concat(errors);
    }
    if (atStart.maxScroll <= 0) {
      failures.push(
        `the strip does not overflow at 380px (maxScroll=${atStart.maxScroll}) — ` +
        `this scenario cannot test the affordance`,
      );
    } else {
      // Parked at the left: content hidden to the RIGHT only.
      if (atStart.start !== "0") {
        failures.push(`at scrollLeft=0 the start edge must not be faded (got data-scroll-start=${atStart.start})`);
      }
      if (atStart.end !== "1") {
        failures.push(`at scrollLeft=0 with overflow, the end edge must be faded (got data-scroll-end=${atStart.end})`);
      }
      if (!atStart.mask.includes("gradient")) {
        failures.push(`no mask gradient applied while the strip overflows: ${atStart.mask}`);
      }

      // Scroll to the far end: content hidden to the LEFT only.
      await page.evaluate(() => {
        const bar = document.querySelector(".task .tabs");
        bar.scrollLeft = bar.scrollWidth;
      });
      await page.waitForTimeout(250);
      const atEnd = await readHints(page);
      if (atEnd.start !== "1") {
        failures.push(`scrolled to the end, the start edge must be faded (got data-scroll-start=${atEnd.start})`);
      }
      if (atEnd.end !== "0") {
        failures.push(`scrolled to the end, the end edge must not be faded (got data-scroll-end=${atEnd.end})`);
      }

      // Mid-scroll: both edges.
      await page.evaluate(() => {
        const bar = document.querySelector(".task .tabs");
        bar.scrollLeft = Math.round((bar.scrollWidth - bar.clientWidth) / 2);
      });
      await page.waitForTimeout(250);
      const mid = await readHints(page);
      if (mid.start !== "1" || mid.end !== "1") {
        failures.push(
          `mid-scroll both edges must be faded (got start=${mid.start} end=${mid.end})`,
        );
      }

      await screenshot(page, "tabs-scroll-affordance-380");
      // Also shoot the strip alone: the page-level shot is dominated by the
      // form above the card, and the fade is a ~24px detail at its edges.
      const bar = await page.$(".task .tabs");
      if (bar) {
        await bar.screenshot({ path: "/tmp/vts-ui-verify/tabs-strip-mid-scroll.png" });
      }
    }

    // The affordance must not itself create page-level horizontal scroll.
    const pageScroll = await page.evaluate(() => {
      const de = document.scrollingElement || document.documentElement;
      const before = de.scrollLeft;
      de.scrollLeft = 9999;
      const max = de.scrollLeft;
      de.scrollLeft = before;
      return max;
    });
    if (pageScroll !== 0) {
      failures.push(`the page scrolls horizontally by ${pageScroll}px with the tab strip rendered`);
    }
    failures.push(...errors);
    await page.close();

    // --- Wide: the strip fits, so neither edge may be faded. A permanently
    // faded edge would look like a rendering artefact on desktop. ---
    const wide = await browser.newPage({ viewport: { width: 1400, height: 900 }, locale: "ru-RU" });
    await wide.goto(baseUrl, { waitUntil: "networkidle" });
    await wide.waitForTimeout(500);
    await clickReal(wide, ".task .toggle-btn");
    await wide.waitForTimeout(500);
    const w = await readHints(wide);
    if (w && w.maxScroll <= 1) {
      if (w.start !== "0" || w.end !== "0") {
        failures.push(
          `the strip fits at 1400px but an edge is still faded (start=${w.start} end=${w.end})`,
        );
      }
    }
    await wide.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

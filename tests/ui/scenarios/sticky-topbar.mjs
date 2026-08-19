// The app bar is sticky (redesign v2 — .topbar in docs/design-v2/vts-theme.css):
// it spans the viewport, stays pinned while the page scrolls under it, and is
// translucent with a blur so movement underneath reads as motion rather than as
// a solid lid.
//
// The bar lives OUTSIDE .layout, because that box is width-constrained — a
// sticky header inside it would be a 1100px strip with the page visible either
// side. Its inner div re-applies the page column, so the two things this
// scenario has to hold together are "bar spans the viewport" and "bar contents
// line up with the cards".
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "sticky-topbar";

const iso = new Date().toISOString();
// Enough cards that the page genuinely scrolls at every width tested.
const TASKS = Array.from({ length: 14 }, (_, i) => ({
  id: `t${i}`, status: "completed",
  source_url: `https://youtube.com/watch?v=abc${i}`,
  source_title: `Task ${i}`, display_name: `Task ${i}`,
  created_at: new Date(Date.now() - i * 60000).toISOString(), updated_at: iso,
  media_path: "/m.mp4", options: { transcript: true, prompts: [] }, steps: [],
}));

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const browser = await launch();
  const failures = [];
  try {
    for (const width of [1150, 412, 320]) {
      const { page, errors } = await openPage(browser, baseUrl, { width, height: 800 });

      const before = await page.evaluate(() => {
        const bar = document.querySelector(".topbar");
        if (!bar) return null;
        const r = bar.getBoundingClientRect();
        const inner = document.querySelector(".topbar-inner")?.getBoundingClientRect();
        const card = document.querySelector(".card")?.getBoundingClientRect();
        const cs = getComputedStyle(bar);
        return {
          position: cs.position,
          top: Math.round(r.top),
          width: Math.round(r.width),
          viewport: document.documentElement.clientWidth,
          innerLeft: inner ? Math.round(inner.left) : null,
          cardLeft: card ? Math.round(card.left) : null,
          background: cs.backgroundColor,
          blur: cs.backdropFilter + "|" + cs.webkitBackdropFilter,
          scrollable: document.documentElement.scrollHeight > document.documentElement.clientHeight,
        };
      });

      if (!before) { failures.push(`[${width}px] no .topbar found`); await page.close(); continue; }
      if (before.position !== "sticky") {
        failures.push(`[${width}px] .topbar is ${before.position}, not sticky`);
      }
      // Full-bleed: the bar must reach both viewport edges, not the page column.
      if (before.width !== before.viewport) {
        failures.push(`[${width}px] the bar is ${before.width}px in a ${before.viewport}px viewport — it must span the full width`);
      }
      // ...while its CONTENTS stay on the page column, or the brand would not
      // line up with the cards below it.
      if (before.innerLeft !== null && before.cardLeft !== null && before.innerLeft !== before.cardLeft) {
        failures.push(`[${width}px] bar contents start at ${before.innerLeft} but cards at ${before.cardLeft}`);
      }
      // Translucent, not opaque: an alpha channel below 1 is what lets the
      // content show through the blur.
      const alpha = before.background.startsWith("rgba(")
        ? parseFloat(before.background.split(",")[3])
        : 1;
      if (!(alpha < 1)) {
        failures.push(`[${width}px] the bar is opaque (${before.background}) — the design calls for translucency`);
      }
      if (!/blur/.test(before.blur)) {
        failures.push(`[${width}px] the bar has no backdrop blur (${before.blur})`);
      }

      // The actual behaviour: content scrolls UNDER a bar that does not move.
      if (!before.scrollable) {
        failures.push(`[${width}px] the page does not scroll — the sticky check would be vacuous`);
      } else {
        await page.evaluate(() => window.scrollTo(0, 400));
        await page.waitForTimeout(250);
        const after = await page.evaluate(() => ({
          top: Math.round(document.querySelector(".topbar").getBoundingClientRect().top),
          scrolled: Math.round(window.scrollY),
        }));
        if (after.scrolled < 100) {
          failures.push(`[${width}px] the page did not actually scroll (scrollY ${after.scrolled})`);
        } else if (after.top !== 0) {
          failures.push(`[${width}px] the bar moved to ${after.top} after scrolling ${after.scrolled}px — it must stay pinned`);
        }
      }

      if (errors.length) failures.push(`[${width}px] JS errors: ` + JSON.stringify(errors));
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

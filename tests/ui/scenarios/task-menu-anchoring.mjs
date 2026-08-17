// Two defects in the task kebab, both reported from the shipped build.
//
//  1. The menu was position:fixed with coordinates written by JS at open time.
//     That pins it to the VIEWPORT, so scrolling left it hanging over unrelated
//     cards while the card it belongs to moved away. It is anchored to its
//     wrapper now and travels with the card.
//  2. The "open player" row is an <a> among <button> rows (it opens a real
//     URL), so the UA underlined it and it read as a different kind of entry.
//
// The anchoring check also guards what the fixed positioning was FOR: the panel
// must stay fully on screen at a phone width.
import { startStubServer, launch, openPage, clickReal } from "../harness.mjs";

export const name = "task-menu-anchoring";

const iso = new Date().toISOString();
const TASKS = [...Array(8)].map((_, i) => ({
  id: `t${i}`, status: "completed",
  source_url: `https://y/t${i}`, source_title: `Meeting recording ${i}`,
  display_name: `Meeting recording ${i}`,
  created_at: iso, updated_at: iso, media_path: "/m.mp4",
  options: { transcript: true, prompts: [] }, steps: [],
}));

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const browser = await launch();
  const failures = [];
  try {
    for (const width of [1100, 360]) {
      const { page } = await openPage(browser, baseUrl, { width, height: 700 });
      await clickReal(page, ".task .task-menu-btn");
      await page.waitForTimeout(300);

      const before = await page.evaluate(() => {
        const m = document.querySelector(".task-menu.open");
        if (!m) return null;
        const r = m.getBoundingClientRect();
        return {
          y: Math.round(r.y),
          onScreen: r.left >= 0 && r.right <= window.innerWidth,
          left: Math.round(r.left), right: Math.round(r.right),
        };
      });
      if (!before) {
        failures.push(`[${width}px] task menu did not open`);
        await page.close();
        continue;
      }
      // What position:fixed was there for: the panel must not hang off the edge.
      if (!before.onScreen) {
        failures.push(`[${width}px] menu is off screen (left=${before.left} right=${before.right})`);
      }

      // The whole point: scrolling must move the menu with its card.
      await page.evaluate(() => window.scrollBy(0, 200));
      await page.waitForTimeout(250);
      const after = await page.evaluate(() => {
        const m = document.querySelector(".task-menu.open");
        return m ? Math.round(m.getBoundingClientRect().y) : null;
      });
      if (after === null) {
        failures.push(`[${width}px] menu vanished on scroll`);
      } else if (Math.abs((before.y - after) - 200) > 6) {
        failures.push(
          `[${width}px] menu did not scroll with the card: moved ${before.y - after}px for a 200px scroll ` +
          `— a viewport-fixed panel stays put while the card moves away`
        );
      }

      // Every row reads as the same kind of thing, <a> or <button>.
      const underlined = await page.evaluate(() =>
        [...document.querySelectorAll(".task-menu .menu-item")]
          .filter((el) => getComputedStyle(el).textDecorationLine.includes("underline"))
          .map((el) => el.className)
      );
      if (underlined.length) {
        failures.push(`[${width}px] menu rows must not be underlined: ${JSON.stringify(underlined)}`);
      }
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

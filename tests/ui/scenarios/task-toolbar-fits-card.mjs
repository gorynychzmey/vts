// Regression (vts-nr4), two cosmetic-but-real defects in the task card:
//
//  1. The mobile action toolbar overshot the card by only a few px, so the
//     strip scrolled just far enough to slice the LAST button in half. It was
//     scrollable for no benefit — everything fits once the row is allowed to
//     distribute. Note the scroller holds the runtime clock AND the button row,
//     so measuring only .task-actions-inline misses the real width.
//  2. The rename pencil sat in the flex .task-title-row next to a long, greedy
//     title and got squeezed from its 2.25rem square down to ~16px. Renaming
//     moved into the task menu in redesign v2, so there is no pencil to squash
//     any more — what remains squeezable next to the title is the player glyph,
//     which is asserted instead: it must keep its size AND the title must still
//     truncate rather than push it out.
//
// Asserts the last toolbar button is fully inside the scroller's client box.
import { startStubServer, launch } from "../harness.mjs";

export const name = "task-toolbar-fits-card";

const LONG_URL =
  "https://event.on24.com/eventRegistration/console/apollox/mainEvent" +
  "?simulive=y&eventid=4949988&sessionid=1&username=&format=fhaudio";

// A completed task with every action available — the widest the toolbar gets.
const TASK = {
  id: "t1",
  status: "completed",
  state: "completed",
  title: "Make vibe coding ready for the enterprise (EMEA and APAC timezones)",
  source_url: LONG_URL,
  source_type: "url",
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
  // Needed for the player glyph to render at all: it is shown on exactly the
  // condition that makes the title a link. The field is media_path, not
  // media_ready — runtime.mediaReady is derived from it.
  media_path: "/data/t1/media.mp4",
  size_bytes: 5444141056,
  duration_sec: 2880,
  options: { transcript: true, audio_only: false, diarize: true },
  capabilities: { can_restart_summary: true, can_restart_final_summary: true },
  results: [{ id: "r1", kind: "summary", name: "Zusammenfassung" }],
  steps: [],
};

const WIDTHS = [320, 360, 412];
const LOCALES = ["en-US", "de-DE"];

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  const failures = [];
  try {
    for (const locale of LOCALES) {
      const context = await browser.newContext({ locale, viewport: { width: 360, height: 900 } });
      for (const width of WIDTHS) {
        const page = await context.newPage();
        await page.setViewportSize({ width, height: 900 });
        await page.goto(baseUrl, { waitUntil: "networkidle" });
        await page.waitForTimeout(500);

        const r = await page.evaluate(() => {
          const card = document.querySelector("article.task");
          if (!card) return { missing: true };
          const sc = card.querySelector(".task-right-bottom");
          // The pencil is gone (renaming lives in the task menu now); the glyph
          // inside the title link is the flex item that can still be squeezed.
          const edit = card.querySelector(".task-link .player-glyph");
          const buttons = [...card.querySelectorAll(".task-actions-inline > *")]
            .filter((e) => getComputedStyle(e).display !== "none");
          const last = buttons[buttons.length - 1];
          const scRect = sc ? sc.getBoundingClientRect() : null;
          const lastRect = last ? last.getBoundingClientRect() : null;
          const editRect = edit ? edit.getBoundingClientRect() : null;
          return {
            toolbarOverflow: sc ? sc.scrollWidth - sc.clientWidth : null,
            // Fully visible = the last button's right edge is inside the
            // scroller's visible box, not merely present in its scroll area.
            lastFullyVisible: scRect && lastRect
              ? lastRect.right <= scRect.right + 0.5
              : null,
            buttonCount: buttons.length,
            editW: editRect ? Math.round(editRect.width) : null,
            editH: editRect ? Math.round(editRect.height) : null,
          };
        });

        if (r.missing) {
          failures.push(`[${locale}@${width}] no task card rendered`);
          await page.close();
          continue;
        }
        if (r.toolbarOverflow > 0) {
          failures.push(
            `[${locale}@${width}] task toolbar overflows its card by ${r.toolbarOverflow}px ` +
            `(${r.buttonCount} buttons) — it scrolls to reveal a sliver of the last one`
          );
        }
        if (r.lastFullyVisible === false) {
          failures.push(`[${locale}@${width}] last toolbar button is only partially visible`);
        }
        // The glyph is ~13px and flex:0 0 auto; anything under 10 means the
        // title started eating it instead of truncating itself.
        if (r.editW !== null && r.editW < 10) {
          failures.push(
            `[${locale}@${width}] player glyph squashed to ${r.editW}x${r.editH}px ` +
            `(expected ~13px — it must not shrink next to the title)`
          );
        }
        await page.close();
      }
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

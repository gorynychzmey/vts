// Regression (vts-nr4): the page scrolled sideways on a phone even though
// nothing looked out of place. Two independent causes, both invisible to a
// "does it look right" screenshot check:
//
//  1. [data-tooltip]::after was laid out at `left: 50%` with `width: max-content`
//     (up to 16rem). On a ~150px control that box sticks out ~125px past the
//     trigger, and `translateX(-50%)` moves only the PAINTED pixels — the
//     pre-transform box is what counts towards scrollWidth. The bubbles are
//     always in the DOM, so the page scrolled even while they were invisible.
//  2. .task-list is a grid whose implicit track is `auto` = max-content of the
//     widest card. `min-width: 0` on the CONTAINER does not constrain the TRACK,
//     so a long title/URL made the card wider than the viewport.
//
// Asserts the user-facing symptom directly (can the document actually be
// scrolled left?) across narrow widths, rather than any one CSS property — the
// property that broke it is not the property that would break it next time.
import { startStubServer, launch } from "../harness.mjs";

export const name = "mobile-no-horizontal-scroll";

const LONG_URL =
  "https://event.on24.com/eventRegistration/console/apollox/mainEvent" +
  "?simulive=y&eventid=4949988&sessionid=1&username=&partnerref=&format=fhaudio";

const mkTask = (id, title, size, status = "completed") => ({
  id,
  status,
  state: status,
  title,
  source_url: LONG_URL,
  source_type: "url",
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
  size_bytes: size,
  duration_sec: 3600,
  options: { transcript: true, audio_only: false, diarize: true },
  results: [{ id: `r${id}`, kind: "summary", name: "Zusammenfassung" }],
  steps: [],
});

const TASKS = [
  mkTask("t1", "Is vibe coding ready for the enterprise (EMEA and APAC timezones)", 5444141056),
  mkTask("t2", "Supercharge your agents and applications with the AI developer platform", 1858076672),
  mkTask("t3", "Kurz", 1024, "running"),
];

// 320 = smallest phone still in use; 360/412 = the common Android widths.
const WIDTHS = [320, 360, 412];
// German has the longest option/menu labels, Russian the longest task actions.
const LOCALES = ["en-US", "de-DE", "ru-RU"];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": TASKS,
    "/api/push/config": { enabled: true },
  });
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

        const r = await page.evaluate((vw) => {
          // The symptom itself: try to scroll right and see if anything moved.
          const de = document.scrollingElement || document.documentElement;
          const before = de.scrollLeft;
          de.scrollLeft = 9999;
          const maxScrollLeft = de.scrollLeft;
          de.scrollLeft = before;

          // Content inside a horizontal scroller (the task toolbar) is allowed
          // to exceed the viewport — that is the scroller doing its job.
          const inScroller = (el) => {
            let n = el.parentElement;
            while (n && n !== document.body) {
              const ox = getComputedStyle(n).overflowX;
              if (ox === "auto" || ox === "scroll" || ox === "hidden") return true;
              n = n.parentElement;
            }
            return false;
          };

          const escapes = [];
          for (const el of document.querySelectorAll("body *")) {
            const cs = getComputedStyle(el);
            if (cs.display === "none" || cs.visibility === "hidden") continue;
            // .bg-shape is a fixed decorative blob; fixed elements cannot
            // extend the scrollable area.
            if (cs.position === "fixed") continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            if (rect.right > vw + 0.5 && !inScroller(el)) {
              const cls = typeof el.className === "string"
                ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".")
                : "";
              escapes.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${cls}@${Math.round(rect.right)}`);
            }
          }
          return { maxScrollLeft, escapes: escapes.slice(0, 5), lang: document.documentElement.lang };
        }, width);

        if (r.maxScrollLeft !== 0) {
          failures.push(
            `[${locale}@${width}] page scrolls horizontally by ${r.maxScrollLeft}px ` +
            `(offenders: ${r.escapes.join(", ") || "none visibly past the edge"})`
          );
        }
        if (r.escapes.length) {
          failures.push(`[${locale}@${width}] elements past the viewport edge: ${r.escapes.join(", ")}`);
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

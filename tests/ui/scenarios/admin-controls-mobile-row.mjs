// The impersonation controls must stay on ONE line on a phone.
//
// Reported from a real phone (360px, v1.7.45): the reset button had dropped
// onto its own line below the user picker, leaving a stray icon floating under
// the row. `.context-line` wraps, and the mobile rule gives the <select>
// `width: 100%`, so the select alone fills the line and both buttons are
// pushed past the wrap.
//
// Measured, not eyeballed: the assertion compares the buttons' vertical centre
// against the select's, which is what "same row" actually means. A screenshot
// would go stale and a class check would not notice a layout regression.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "admin-controls-mobile-row";

const ME = {
  requested_by: "victor@vostrikov.de",
  acting_as: "diana@shliakhtsenia.de",
  is_admin: true,
};

const USERS = [
  { username: "victor@vostrikov.de", is_admin: true },
  { username: "diana@shliakhtsenia.de", is_admin: false },
];

// Phone widths that matter: the reported device, and the narrowest we support.
const WIDTHS = [360, 320];

async function rowGeometry(page) {
  return page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, mid: r.top + r.height / 2 };
    };
    return {
      select: box("#admin-user-select"),
      apply: box("#admin-apply-btn"),
      reset: box("#admin-reset-btn"),
      row: box("#admin-controls"),
      hidden: document.getElementById("admin-controls")?.classList.contains("hidden"),
    };
  });
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/me": ME,
    "/api/admin/users": USERS,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl, { width: 360, height: 800 });

    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 800 });
      await page.waitForTimeout(200);

      const g = await rowGeometry(page);
      if (g.hidden || !g.select || !g.apply || !g.reset) {
        failures.push(`${width}px: admin controls not rendered (${JSON.stringify(g)})`);
        continue;
      }

      // Same row = the buttons' centres sit within the select's vertical span.
      // A wrapped button lands a whole control-height below it.
      for (const [name, b] of [["apply", g.apply], ["reset", g.reset]]) {
        if (b.mid < g.select.top || b.mid > g.select.bottom) {
          failures.push(
            `${width}px: the ${name} button wrapped onto another line ` +
              `(select ${g.select.top.toFixed(0)}-${g.select.bottom.toFixed(0)}, ` +
              `${name} centre ${b.mid.toFixed(0)})`
          );
        }
      }

      // The label may take its own line — at 320px it has to. What must NOT
      // happen is a third line, which is what a wrapped button costs: measure
      // from the top of the select, so the label above it is not counted.
      const controlsHeight = g.row.bottom - g.select.top;
      const selectHeight = g.select.bottom - g.select.top;
      if (controlsHeight > selectHeight * 1.6) {
        failures.push(
          `${width}px: select+buttons occupy ${controlsHeight.toFixed(0)}px for a ` +
            `${selectHeight.toFixed(0)}px control — something wrapped below them`
        );
      }

      // Nothing may hang off the right edge either: fitting on one line by
      // overflowing the viewport is not a fix.
      if (g.reset.right > width + 1) {
        failures.push(`${width}px: the reset button overflows the viewport (right edge ${g.reset.right.toFixed(0)})`);
      }

      // The buttons must keep their tap size — shrinking them to fit would
      // trade one defect for a worse one.
      const resetWidth = g.reset.right - g.reset.left;
      if (resetWidth < 24) {
        failures.push(`${width}px: the reset button shrank to ${resetWidth.toFixed(0)}px wide`);
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

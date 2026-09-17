// The "show more" button must be OFF SCREEN when there is no next page
// (vts-ak6z).
//
// `.btn-text { display: inline-flex }` is an author rule, and an author rule
// always outranks the user agent's `[hidden] { display: none }`. The file
// already cures that trap for the CLASS (`.btn-text.hidden`, from vts-552),
// but these buttons are toggled by the ATTRIBUTE (`more.hidden = …`), and
// `.btn-text[hidden]` was covered nowhere. Measured before the fix: attribute
// set, `display: flex`, `offsetHeight: 34` — a button standing on screen whose
// handler returns immediately. Anyone with fewer tasks than one page saw it.
//
// Both directions are asserted, because a cure written too broadly hides the
// button forever and a one-sided test would not notice: hidden state must be
// invisible, and a real next page must still be reachable.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "load-more-hidden-when-exhausted";

// Every button in the markup that is hidden through the attribute rather than
// the class, so the fix is asserted across the class of defect and not only on
// the one instance that was visible.
const ATTRIBUTE_HIDDEN_BUTTONS = ["#task-load-more", "#library-load-more"];

/** Drive the real sentinel renderer with a chosen paging state. */
const renderWith = (page, hasNextPage) =>
  page.evaluate((hasTail) => {
    state.taskPaging.loading = false;
    state.taskPaging.exhausted = !hasTail;
    state.taskPaging.tail = hasTail
      ? { ts: "2026-07-30T12:34:56.000000+00:00", id: "a1111111-1111-1111-1111-111111111111" }
      : null;
    updateSentinel();
    const el = document.getElementById("task-load-more");
    const cs = getComputedStyle(el);
    return { hiddenAttr: el.hidden, display: cs.display, offsetHeight: el.offsetHeight };
  }, hasNextPage);

export async function run() {
  const failures = [];
  // The default stub serves an empty task list, which is the state the defect
  // was reported in: no tasks, so no next page, so nothing to show.
  const { server, baseUrl } = await startStubServer({
    "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // 1. The production symptom, on a page nobody poked.
    if (await isVisible(page, "#task-load-more")) {
      const state_ = await page.evaluate(() => {
        const el = document.getElementById("task-load-more");
        return {
          hiddenAttr: el.hidden,
          display: getComputedStyle(el).display,
          offsetHeight: el.offsetHeight,
        };
      });
      failures.push(
        `#task-load-more is on screen with an empty task list: ${JSON.stringify(state_)}`
      );
    }

    // 2. Same for every other attribute-hidden button of this class. The
    //    library one is invisible by accident today (its container is hidden),
    //    so it is checked through the attribute + computed display rather than
    //    offsetHeight alone.
    const perButton = await page.evaluate((selectors) => {
      const out = {};
      for (const sel of selectors) {
        const el = document.querySelector(sel);
        out[sel] = el
          ? { present: true, hiddenAttr: el.hidden, display: getComputedStyle(el).display }
          : { present: false };
      }
      return out;
    }, ATTRIBUTE_HIDDEN_BUTTONS);
    for (const [sel, info] of Object.entries(perButton)) {
      if (!info.present) {
        failures.push(`${sel} is missing from the markup — the check below proves nothing`);
        continue;
      }
      if (info.hiddenAttr && info.display !== "none") {
        failures.push(
          `${sel} carries [hidden] but computes display:${info.display} — the ` +
          `attribute does not hide a .btn-text`
        );
      }
    }

    // 3. The other direction: a next page exists, so the button must be
    //    reachable. Without this a cure like `.btn-text { display: none }`
    //    would pass every check above.
    const withNextPage = await renderWith(page, true);
    if (withNextPage.hiddenAttr) {
      failures.push(
        "setup: the renderer hid the button although the state says there is a " +
        "next page — the visibility check below would prove nothing"
      );
    } else if (withNextPage.display === "none" || withNextPage.offsetHeight === 0) {
      failures.push(
        `#task-load-more is unreachable when a next page exists: ` +
        `${JSON.stringify(withNextPage)}`
      );
    }

    // 4. And back, through the same renderer, so the two states are compared
    //    on equal terms.
    const exhausted = await renderWith(page, false);
    if (!exhausted.hiddenAttr) {
      failures.push("setup: the renderer did not hide the button on an exhausted list");
    } else if (exhausted.display !== "none" || exhausted.offsetHeight !== 0) {
      failures.push(
        `#task-load-more stays on screen once the list is exhausted: ` +
        `${JSON.stringify(exhausted)}`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

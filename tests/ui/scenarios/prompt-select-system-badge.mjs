// The vendor prompt must read as the system one, and stay selected by default.
//
// Two regressions from vts-kujy, both from the same root. Making the system
// prompt editable turned it into an ordinary `prompts` row, so GET /api/prompts
// now reports `source: "user"` for it and carries the real answer in a separate
// `is_system` flag. Two places still keyed off `source` alone:
//
//   1. The badge read "user" for the vendor prompt — the user's own prompts and
//      the shipped one became indistinguishable in the picker.
//   2. resetPromptSelection() ticked `source === "system" && id === "summary"`,
//      which now matches nothing, so a new task started with NO prompt selected.
//      That one is the expensive half: it changes what a task produces, and it
//      does it silently.
import { startStubServer, launch, openPage, clickReal, settled, openFromHeaderMenu } from "../harness.mjs";

export const name = "prompt-select-system-badge";

const SYSTEM_ID = "11111111-1111-1111-1111-111111111111";
const USER_ID = "22222222-2222-2222-2222-222222222222";

// Exactly what the API serves today: everything is source "user", and only
// `is_system` tells the vendor copy apart.
const PROMPTS = [
  { source: "user", id: SYSTEM_ID, name: "Summary", editable: true, is_system: true },
  { source: "user", id: USER_ID, name: "Memo", editable: true, is_system: false },
];

const rows = (page) =>
  page.evaluate(() =>
    [...document.querySelectorAll("#prompt-select .prompt-select-popover label")].map((l) => ({
      name: (l.querySelector(".prompt-name")?.textContent || "").trim(),
      badge: (l.querySelector(".prompt-badge")?.textContent || "").trim(),
      badgeCls: l.querySelector(".prompt-badge")?.className || "",
      checked: !!l.querySelector('input[type="checkbox"]')?.checked,
      id: l.querySelector('input[type="checkbox"]')?.dataset.id || "",
    }))
  );

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/prompts": PROMPTS });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    await clickReal(page, "#prompt-select .prompt-select-toggle");
    await settled(page);

    const list = await rows(page);
    if (list.length !== 2) {
      failures.push(`expected 2 prompts in the picker, got ${list.length}: ${JSON.stringify(list)}`);
      return failures;
    }

    const system = list.find((r) => r.id === SYSTEM_ID);
    const user = list.find((r) => r.id === USER_ID);

    // --- 1. The badge tells the two apart ---
    if (system.badge.toLowerCase() !== "system") {
      failures.push(
        `the vendor prompt is badged ${JSON.stringify(system.badge)}, expected "system"`
      );
    }
    if (!/prompt-badge-system/.test(system.badgeCls)) {
      failures.push(`the vendor prompt's badge class is ${JSON.stringify(system.badgeCls)}`);
    }
    // A real user prompt must NOT be relabelled by the same change.
    if (user.badge.toLowerCase() !== "user") {
      failures.push(`an ordinary prompt is badged ${JSON.stringify(user.badge)}, expected "user"`);
    }
    if (/prompt-badge-system/.test(user.badgeCls)) {
      failures.push("an ordinary prompt got the system badge class");
    }

    // --- 2. It is selected by default ---
    // This is what a new task actually starts with; unticked means the task
    // silently produces no summary.
    if (!system.checked) {
      failures.push("the vendor prompt is NOT selected by default — a new task would run with no prompt");
    }
    if (user.checked) {
      failures.push("an ordinary user prompt was selected by default");
    }

    // --- 3. The manager dialog's buttons stay on screen on a phone ---
    // Reported from a real device: with four buttons and translated labels the
    // row did not wrap, so two of them ran past the right edge and could not be
    // tapped at all. Measured at 360px before the fix: 383 and 458 against a
    // 360px viewport.
    await page.setViewportSize({ width: 360, height: 800 });
    await openFromHeaderMenu(page, "#prompts-btn");
    await clickReal(page, "#prompts-list .mgr-item");
    await settled(page);
    const offscreen = await page.evaluate(() => {
      const row = document.querySelector(".prompt-form-actions");
      if (!row) return "no .prompt-form-actions row";
      return [...row.querySelectorAll("button")]
        .filter((b) => b.offsetParent !== null)
        .filter((b) => b.getBoundingClientRect().right > window.innerWidth + 1)
        .map((b) => `${b.textContent.trim()} (right ${Math.round(b.getBoundingClientRect().right)})`);
    });
    if (typeof offscreen === "string") failures.push(offscreen);
    else if (offscreen.length) {
      failures.push(`buttons run past the right edge at 360px: ${JSON.stringify(offscreen)}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

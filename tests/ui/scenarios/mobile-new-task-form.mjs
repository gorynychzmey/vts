// Verifies the mobile (375px) New Task form layout (vts-wjb):
//  - submit "+" stays inline with the URL input (not orphaned below)
//  - .options-row becomes a CSS grid at <=760px
//  - the two option pills (audio_only / transcript) sit on the same row
//  - saving a preset is not dressed as the primary action (all widths).
//    It used to be a chip next to the selector, asserted as the neutral #efe9db
//    so it would not compete with the submit "+". In redesign v2 it is a row
//    inside the preset menu, so the check is that it stays a menu row — its
//    background is transparent by design — and specifically NOT the accent
//    fill, which is what the original assertion was guarding against.
import { startStubServer, launch } from "../harness.mjs";

export const name = "mobile-new-task-form";

const ACCENT = "rgb(197, 83, 42)"; // --accent: saving a preset must never look
                                   // like the primary action

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    // ---- Mobile: 375px ----
    const mobile = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await mobile.goto(baseUrl, { waitUntil: "networkidle" });
    await mobile.waitForTimeout(300);
    await mobile.evaluate(() => document.getElementById("task-form").scrollIntoView());
    await mobile.waitForTimeout(100);

    const m = await mobile.evaluate(() => {
      const top = (sel) => {
        const el = document.querySelector(sel);
        return el ? Math.round(el.getBoundingClientRect().top) : null;
      };
      // Checkbox pills only: .option-pill also matches the preset pill now
      // (redesign v2), which is a full-width row of its own, so including it
      // would compare a row against a half-row.
      const pills = [...document.querySelectorAll("#task-form .option-pill:has(input[type=checkbox])")];
      const pillTops = pills.map((p) => Math.round(p.getBoundingClientRect().top));
      return {
        urlTop: top("#task-form #url"),
        submitTop: top("#task-form #submit-btn"),
        optionsDisplay: getComputedStyle(document.querySelector("#task-form .options-row")).display,
        saveBg: getComputedStyle(document.getElementById("preset-save-btn")).backgroundColor,
        pillCount: pills.length,
        pillTops,
      };
    });

    if (m.urlTop === null || m.submitTop === null) {
      failures.push("mobile: #url or #submit-btn not found");
    } else if (Math.abs(m.submitTop - m.urlTop) >= 20) {
      failures.push(`mobile: submit "+" orphaned below url (urlTop=${m.urlTop} submitTop=${m.submitTop})`);
    }
    if (m.optionsDisplay !== "grid") {
      failures.push(`mobile: .options-row display should be grid, got ${m.optionsDisplay}`);
    }
    if (m.saveBg === ACCENT) {
      failures.push(`mobile: #preset-save-btn must not wear the accent fill, got ${m.saveBg}`);
    }
    if (m.pillCount < 2) {
      failures.push(`mobile: expected >=2 option pills, got ${m.pillCount}`);
    } else if (Math.abs(m.pillTops[0] - m.pillTops[1]) >= 20) {
      failures.push(`mobile: audio_only/transcript pills not on same row (tops=${JSON.stringify(m.pillTops.slice(0, 2))})`);
    }

    const shot = "/tmp/vts-mobile-new-task-form.png";
    await mobile.locator("#task-form").screenshot({ path: shot });
    await mobile.close();

    // ---- Desktop: 1100px (unchanged layout, neutral save btn) ----
    const desktop = await browser.newPage({ viewport: { width: 1100, height: 700 } });
    await desktop.goto(baseUrl, { waitUntil: "networkidle" });
    await desktop.waitForTimeout(300);
    const d = await desktop.evaluate(() => ({
      optionsDisplay: getComputedStyle(document.querySelector("#task-form .options-row")).display,
      saveBg: getComputedStyle(document.getElementById("preset-save-btn")).backgroundColor,
    }));
    if (d.optionsDisplay !== "flex") {
      failures.push(`desktop: .options-row display should be flex, got ${d.optionsDisplay}`);
    }
    if (d.saveBg === ACCENT) {
      failures.push(`desktop: #preset-save-btn must not wear the accent fill, got ${d.saveBg}`);
    }
    await desktop.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

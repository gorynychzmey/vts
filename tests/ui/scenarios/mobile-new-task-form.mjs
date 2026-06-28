// Verifies the mobile (375px) New Task form layout (vts-wjb):
//  - submit "+" stays inline with the URL input (not orphaned below)
//  - .options-row becomes a CSS grid at <=760px
//  - the two option pills (audio_only / transcript) sit on the same row
//  - #preset-save-btn is the neutral #efe9db, not the orange accent (all widths)
// Also checks desktop (1100px) is unchanged: .options-row is flex, save btn neutral.
import { startStubServer, launch } from "../harness.mjs";

export const name = "mobile-new-task-form";

const NEUTRAL = "rgb(239, 233, 219)"; // #efe9db

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
      const pills = [...document.querySelectorAll("#task-form .option-pill")];
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
    if (m.saveBg !== NEUTRAL) {
      failures.push(`mobile: #preset-save-btn background should be ${NEUTRAL}, got ${m.saveBg}`);
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
    if (d.saveBg !== NEUTRAL) {
      failures.push(`desktop: #preset-save-btn background should be ${NEUTRAL}, got ${d.saveBg}`);
    }
    await desktop.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

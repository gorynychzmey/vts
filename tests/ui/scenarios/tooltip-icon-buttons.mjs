// Verifies the reusable [data-tooltip] pattern on the icon action buttons in
// the prompt + preset manager dialogs. The native `title` does nothing on
// touch, so the bubble must show on hover (desktop) AND focus/active (tap).
// Asserts: each action icon button has a non-empty data-tooltip; the ::after
// opacity is "0" at rest, "1" on hover, and "1" on focus (the touch-tap path).
// Captures readable desktop (1100px) + mobile (375px) screenshots into /tmp.
import { chromium } from "playwright";
import fs from "fs";
import { startStubServer } from "../harness.mjs";

export const name = "tooltip-icon-buttons";

const SHOT_DIR = "/tmp/vts-ui-verify";

async function afterOpacity(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return getComputedStyle(el, "::after").opacity;
  }, selector);
}

async function checkDialog(page, listSelector, label) {
  const failures = [];
  const sel = `${listSelector} .prompts-actions .icon-btn[data-tooltip]`;
  const btn = await page.$(sel);
  if (!btn) {
    failures.push(`${label}: no icon button with [data-tooltip] found`);
    return failures;
  }

  // Non-empty data-tooltip attribute.
  const tip = await btn.evaluate((b) => b.getAttribute("data-tooltip") || "");
  if (!tip.trim()) failures.push(`${label}: data-tooltip is empty`);

  // At rest: bubble hidden.
  await page.mouse.move(0, 0);
  await page.$eval(sel, (b) => b.blur());
  await page.waitForTimeout(80);
  const rest = await afterOpacity(page, sel);
  if (rest !== "0") failures.push(`${label}: ::after opacity at rest expected "0", got "${rest}"`);

  // Hover (desktop path). The opacity transition (0.12s) may be mid-flight,
  // so assert "visible" (> 0.5) rather than exactly "1" to avoid a race.
  await page.hover(sel);
  await page.waitForTimeout(200);
  const hov = await afterOpacity(page, sel);
  if (parseFloat(hov) <= 0.5) failures.push(`${label}: ::after not visible on hover (opacity "${hov}")`);

  // Move away, then focus (touch-tap path — buttons get focus on tap).
  await page.mouse.move(0, 0);
  await page.waitForTimeout(80);
  await page.$eval(sel, (b) => b.focus());
  await page.waitForTimeout(200);
  const foc = await afterOpacity(page, sel);
  if (parseFloat(foc) <= 0.5) failures.push(`${label}: ::after not visible on focus (opacity "${foc}")`);

  return failures;
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/prompts": [
      { source: "system", id: "summary", name: "Summary", editable: false },
      { source: "user", id: "u1", name: "Memo", editable: true },
    ],
    "/api/presets": [
      {
        source: "user",
        id: "p1",
        name: "Standard",
        editable: true,
        options: { language: "ru", audio_only: false, transcript: true, prompts: [] },
      },
    ],
    "/api/me/default_preset": { source: "user", id: "p1" },
  });
  const browser = await chromium.launch();
  const failures = [];
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  try {
    // ---- DESKTOP (1100px): prompts + presets, hover bubble visible ----
    const desktop = await browser.newPage({ viewport: { width: 1100, height: 760 } });
    await desktop.goto(baseUrl, { waitUntil: "networkidle" });
    await desktop.waitForTimeout(300);

    await desktop.click("#prompts-btn");
    await desktop.waitForTimeout(200);
    failures.push(...await checkDialog(desktop, "#prompts-list", "prompts/desktop"));
    // Leave a hover bubble showing for the screenshot.
    await desktop.hover("#prompts-list .prompts-actions .icon-btn[data-tooltip]");
    await desktop.waitForTimeout(150);
    await desktop.screenshot({ path: `${SHOT_DIR}/tooltip-prompts-desktop.png` });
    await desktop.click("#prompts-close-btn").catch(() => {});
    await desktop.waitForTimeout(150);

    await desktop.click("#presets-btn");
    await desktop.waitForTimeout(200);
    failures.push(...await checkDialog(desktop, "#presets-list", "presets/desktop"));
    await desktop.hover("#presets-list .prompts-actions .icon-btn[data-tooltip]");
    await desktop.waitForTimeout(150);
    await desktop.screenshot({ path: `${SHOT_DIR}/tooltip-presets-desktop.png` });
    await desktop.close();

    // ---- MOBILE (375px): focus bubble visible (the touch-tap path) ----
    const mobile = await browser.newPage({ viewport: { width: 375, height: 760 } });
    await mobile.goto(baseUrl, { waitUntil: "networkidle" });
    await mobile.waitForTimeout(300);

    await mobile.click("#presets-btn");
    await mobile.waitForTimeout(200);
    failures.push(...await checkDialog(mobile, "#presets-list", "presets/mobile"));
    // Focus the button so the bubble shows (simulated tap) for the screenshot.
    await mobile.$eval("#presets-list .prompts-actions .icon-btn[data-tooltip]", (b) => b.focus());
    await mobile.waitForTimeout(150);
    await mobile.screenshot({ path: `${SHOT_DIR}/tooltip-presets-mobile.png` });
    await mobile.click("#presets-close-btn").catch(() => {});
    await mobile.waitForTimeout(150);

    await mobile.click("#prompts-btn");
    await mobile.waitForTimeout(200);
    failures.push(...await checkDialog(mobile, "#prompts-list", "prompts/mobile"));
    await mobile.$eval("#prompts-list .prompts-actions .icon-btn[data-tooltip]", (b) => b.focus());
    await mobile.waitForTimeout(150);
    await mobile.screenshot({ path: `${SHOT_DIR}/tooltip-prompts-mobile.png` });
    await mobile.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

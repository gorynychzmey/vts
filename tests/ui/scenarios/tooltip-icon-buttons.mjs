// Verifies the reusable [data-tooltip] pattern on per-row icon action buttons.
//
// Host: the VOICE REGISTRY dialog. Both manager dialogs that used to carry this
// pattern have since moved their actions off the rows into the editor beside the
// list (prompts first, then presets), leaving no per-row icon buttons to hover.
// The registry's rows are different in kind — rename and delete act on that
// person, not on something open elsewhere — so its buttons are a stable host
// rather than the next thing to be redesigned away.
// Rehosting, deliberately, instead of loosening the assertion: the pattern is
// still real and still needs a guard. The native `title` does nothing on
// touch, so the bubble must show on hover (desktop) AND focus/active (tap).
// Asserts: each action icon button has a non-empty data-tooltip; the ::after
// opacity is "0" at rest, "1" on hover, and "1" on focus (the touch-tap path).
// Captures readable desktop (1100px) + mobile (375px) screenshots into /tmp.
import { chromium } from "playwright";
import fs from "fs";
import { startStubServer, openFromHeaderMenu } from "../harness.mjs";

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
  const sel = `${listSelector} .speaker-actions .icon-btn[data-tooltip]`;
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

  // Hover (desktop path). Tooltips now have a ~0.5s show-delay, so wait past it
  // (plus the 0.12s fade) before asserting; assert "visible" (> 0.5) rather than
  // exactly "1" to avoid a fade-race.
  await page.hover(sel);
  await page.waitForTimeout(800);
  const hov = await afterOpacity(page, sel);
  if (parseFloat(hov) <= 0.5) failures.push(`${label}: ::after not visible on hover (opacity "${hov}")`);

  // Move away, then focus (touch-tap path — buttons get focus on tap). This is a
  // later, user-initiated focus (well outside the dialog-open window), so the
  // open-time autofocus blur must NOT apply here. Wait past the show-delay too.
  await page.mouse.move(0, 0);
  await page.waitForTimeout(80);
  await page.$eval(sel, (b) => b.focus());
  await page.waitForTimeout(800);
  const foc = await afterOpacity(page, sel);
  if (parseFloat(foc) <= 0.5) failures.push(`${label}: ::after not visible on focus (opacity "${foc}")`);

  return failures;
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/speakers": [
      { id: "s1", name: "Vasya", sample_count: 2 },
      { id: "s2", name: "Petya", sample_count: 0 },
    ],
    "/api/speakers/s1/samples": [],
    "/api/speakers/s2/samples": [],
  });
  const browser = await chromium.launch();
  const failures = [];
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  try {
    // ---- DESKTOP (1100px): registry, hover bubble visible ----
    const desktop = await browser.newPage({ viewport: { width: 1100, height: 760 } });
    await desktop.goto(baseUrl, { waitUntil: "networkidle" });
    await desktop.waitForTimeout(300);

    await openFromHeaderMenu(desktop, "#speaker-registry-btn");
    failures.push(...await checkDialog(desktop, "#speaker-list", "registry/desktop"));
    await desktop.hover("#speaker-list .speaker-actions .icon-btn[data-tooltip]");
    await desktop.waitForTimeout(150);
    await desktop.screenshot({ path: `${SHOT_DIR}/tooltip-registry-desktop.png` });
    await desktop.close();

    // ---- MOBILE (375px): focus bubble visible (the touch-tap path) ----
    const mobile = await browser.newPage({ viewport: { width: 375, height: 760 } });
    await mobile.goto(baseUrl, { waitUntil: "networkidle" });
    await mobile.waitForTimeout(300);

    await openFromHeaderMenu(mobile, "#speaker-registry-btn");
    failures.push(...await checkDialog(mobile, "#speaker-list", "registry/mobile"));
    // Focus the button so the bubble shows (simulated tap) for the screenshot.
    await mobile.$eval("#speaker-list .speaker-actions .icon-btn[data-tooltip]", (b) => b.focus());
    await mobile.waitForTimeout(150);
    await mobile.screenshot({ path: `${SHOT_DIR}/tooltip-registry-mobile.png` });
    await mobile.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

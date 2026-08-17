// Redesign v2, stage 1: the theme is variables only. Asserts the three
// selectors that carry it actually resolve in a real browser:
//   :root                        -> light
//   :root[data-theme="dark"]     -> manual dark
//   @media (prefers-color-scheme: dark) :root:not([data-theme="light"])
//                                -> follow the OS, unless light was chosen
// The contrast assertions are the point: a token set can be installed
// "successfully" and still leave dark-on-dark text, which only a computed
// luminance check catches.
import { startStubServer, launch, screenshot } from "../harness.mjs";
import { chromium } from "playwright";

export const name = "theme-tokens";

// WCAG relative luminance + contrast ratio, from computed rgb() strings.
const LUM = `(rgb) => {
  const [r,g,b] = rgb.match(/\\d+(\\.\\d+)?/g).slice(0,3).map(Number);
  const f = (c) => { c /= 255; return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); };
  return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b);
}`;

async function probe(page) {
  return page.evaluate(`(() => {
    const lum = ${LUM};
    const ratio = (a, b) => {
      const [l1, l2] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (l1 + 0.05) / (l2 + 0.05);
    };
    const cs = getComputedStyle(document.documentElement);
    const body = getComputedStyle(document.body);
    const out = {
      bg: cs.getPropertyValue('--bg').trim(),
      colorScheme: cs.colorScheme,
      bodyColor: body.color,
      bodyBg: body.backgroundColor,
      contrast: [],
    };
    // Sample real text against its own painted background, walking up for a
    // non-transparent ancestor the way the eye does.
    const paintedBg = (el) => {
      for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
        const c = getComputedStyle(n).backgroundColor;
        if (c && c !== 'rgba(0, 0, 0, 0)' && !c.startsWith('rgba(0, 0, 0, 0)')) return c;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
    for (const sel of ['h1', '.preset-label', '#url', '.task-empty', 'button#submit-btn']) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || !el.offsetHeight) continue;
      out.contrast.push({ sel, ratio: +ratio(st.color, paintedBg(el)).toFixed(2) });
    }
    return out;
  })()`);
}

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    // --- (1) light, no OS preference ---
    const light = await browser.newPage({ viewport: { width: 1100, height: 700 }, colorScheme: "light" });
    const errs = [];
    light.on("pageerror", (e) => errs.push("pageerror: " + e.message));
    light.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) errs.push("console.error: " + m.text());
    });
    await light.goto(baseUrl, { waitUntil: "networkidle" });
    await light.waitForTimeout(300);
    if (errs.length) failures.push("JS errors on boot: " + JSON.stringify(errs));

    const l = await probe(light);
    if (l.bg !== "#efeae0") failures.push(`light --bg = ${l.bg}, expected #efeae0`);
    if (l.colorScheme !== "light") failures.push(`light color-scheme = ${l.colorScheme}`);
    for (const c of l.contrast) {
      if (c.ratio < 4.5) failures.push(`light contrast ${c.sel} = ${c.ratio}:1 (< 4.5 AA)`);
    }
    await screenshot(light, "theme-light");

    // --- (2) manual dark, OS still light ---
    await light.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    await light.waitForTimeout(150);
    const d = await probe(light);
    if (d.bg !== "#191512") failures.push(`manual dark --bg = ${d.bg}, expected #191512`);
    if (d.colorScheme !== "dark") failures.push(`manual dark color-scheme = ${d.colorScheme}`);
    // Warm, not grey: red channel must lead blue on the painted background.
    const [dr, , db] = d.bodyBg.match(/\d+/g).map(Number);
    if (!(dr > db)) failures.push(`dark bg ${d.bodyBg} is not warm (r must exceed b)`);
    for (const c of d.contrast) {
      if (c.ratio < 4.5) failures.push(`dark contrast ${c.sel} = ${c.ratio}:1 (< 4.5 AA)`);
    }
    await screenshot(light, "theme-dark");
    await light.close();

    // --- (3) OS dark, no attribute -> follows the OS ---
    const osDark = await browser.newPage({ viewport: { width: 1100, height: 700 }, colorScheme: "dark" });
    await osDark.goto(baseUrl, { waitUntil: "networkidle" });
    await osDark.waitForTimeout(300);
    const o = await probe(osDark);
    if (o.bg !== "#191512") failures.push(`OS-dark (no attribute) --bg = ${o.bg}, expected #191512`);

    // --- (4) OS dark, but light forced -> light wins ---
    await osDark.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
    await osDark.waitForTimeout(150);
    const f = await probe(osDark);
    if (f.bg !== "#efeae0") failures.push(`forced light under OS-dark --bg = ${f.bg}, expected #efeae0`);
    if (f.colorScheme !== "light") failures.push(`forced light color-scheme = ${f.colorScheme}`);
    await osDark.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

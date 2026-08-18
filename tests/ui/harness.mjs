import { chromium } from "playwright";
import http from "http";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// Resolve relative to THIS file (tests/ui/harness.mjs) so the verifier serves
// the static assets of whatever checkout it runs in — the main repo OR a git
// worktree. A hardcoded absolute path silently tested the wrong tree from a
// worktree (VOS-84), passing against feature-less code.
const _here = path.dirname(fileURLToPath(import.meta.url));
export const STATIC_DIR = path.resolve(_here, "..", "..", "vts", "static");

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
  // Fonts are self-hosted now; served as text/plain the browser rejects them.
  ".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png",
};

export const DEFAULT_API = {
  "/api/version": { version: "verify" },
  "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
  "/api/push/config": { enabled: false },
  "/api/tasks": [],
  "/api/prompts": [
    { source: "system", id: "summary", name: "Summary", editable: false },
    { source: "user", id: "u1", name: "Memo", editable: true },
  ],
};

// overrides: { "/api/...": value }  (value = JSON-serializable). Also supports
// an optional `__extraCss` key: a CSS string injected before </head>, used by
// the self-check to simulate a regression.
export async function startStubServer(overrides = {}) {
  const extraCss = overrides.__extraCss || "";
  const api = { ...DEFAULT_API, ...overrides };
  delete api.__extraCss;
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      // Write calls: return a 200 stub so submit/POST flows complete.
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : {}));
      return;
    }
    let f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    // Binary assets (fonts, icons) must not be round-tripped through a string:
    // toString() mangles them and the browser rejects the result.
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") {
      res.setHeader("Content-Type", CT[ext]);
      res.end(fs.readFileSync(fp));
      return;
    }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") {
      body = body.replaceAll("__VTS_VERSION__", "verify");
      if (extraCss) body = body.replace("</head>", `<style id="verify-extra">${extraCss}</style></head>`);
    }
    res.setHeader("Content-Type", CT[path.extname(fp)] || "text/plain");
    res.end(body);
  });
  await new Promise((r) => server.listen(0, r));
  const port = server.address().port;
  return { server, baseUrl: `http://localhost:${port}`, port };
}

export async function launch() {
  return chromium.launch();
}

// The default viewport is deliberately small (1100x700) — it keeps the smoke set
// honest about cramped layouts. A scenario that drives controls near the bottom
// of the page can pass its own: the task menu opens below the form, and the form
// grows as options are added, so at 700px the menu lands off-screen and clicks
// fail with "element is outside of the viewport" rather than any real bug.
export async function openPage(browser, baseUrl, viewport = { width: 1100, height: 700 }) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("EventSource")) {
      errors.push("console.error: " + m.text());
    }
  });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  return { page, errors };
}

export async function isVisible(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden" && el.offsetHeight > 0;
  }, selector);
}

export async function dialogOpen(page, id) {
  return page.evaluate((i) => {
    const d = document.getElementById(i);
    return !!d && d.open === true;
  }, id);
}

export async function computed(page, selector, prop) {
  return page.evaluate(([sel, p]) => {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el)[p] : null;
  }, [selector, prop]);
}

export async function boundingBox(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) };
  }, selector);
}

export async function clickReal(page, selector) {
  await page.click(selector);
}

// The header's manage entries (prompts, presets, voices, tokens, notifications)
// live inside the burger menu, so they are not clickable until it is open
// (vts-nr4). Opens the menu, then clicks the entry.
export async function openFromHeaderMenu(page, itemSelector) {
  await page.click("#header-menu-btn");
  // The menu opens by toggling a class, so it is usable as soon as the entry is
  // actually clickable. Playwright's actionability check already waits for that,
  // but asserting it explicitly keeps the failure message pointed at the menu
  // rather than at whatever the entry was supposed to open.
  await page.waitForSelector(itemSelector, { state: "visible" });
  await page.click(itemSelector);
  // Several of these entries open their dialog only AFTER awaiting fetches
  // (#presets-btn loads prompts and presets before showModal()), so the dialog
  // is not open when the click returns and settling on the still-unchanged page
  // would resolve immediately. Wait for a dialog to actually be open, which is
  // the postcondition every caller relies on. Bounded and non-fatal: a caller
  // that legitimately opens no dialog (or asserts that none opened) still
  // proceeds and makes its own assertion, exactly as it did before.
  await page
    .waitForFunction(() => [...document.querySelectorAll("dialog")].some((d) => d.open), null, { timeout: 5000 })
    .catch(() => {});
  await settled(page);
}

/** Open a task's About dialog. The card used to carry a clickable stats pill
 *  (duration · size) that opened it; redesign v2 removed the pill to make the
 *  card compact, so the kebab menu row is now the only way in.
 *  `cardSelector` defaults to the first card. */
export async function openTaskAbout(page, cardSelector = ".task") {
  await page.click(`${cardSelector} .task-menu-btn`);
  // `.task-menu.open` IS the menu-opened condition, so wait for the real thing.
  await page.waitForSelector(`${cardSelector} .task-menu.open .task-about-btn`, { state: "visible" });
  await page.click(`${cardSelector} .task-menu.open .task-about-btn`);
  // The About dialog renders its content synchronously once opened; callers read
  // its geometry, so settle rather than sleep.
  await settled(page);
}

const SHOT_DIR = "/tmp/vts-ui-verify";
export async function screenshot(page, name) {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const p = `${SHOT_DIR}/${name}.png`;
  await page.screenshot({ path: p });
  return p;
}

/** Wait for layout to settle instead of sleeping a fixed guess.
 *
 *  Most `waitForTimeout` calls in the scenarios mean "let the DOM finish
 *  reacting", not "this product behaviour genuinely takes N ms". This resolves
 *  as soon as two consecutive animation frames report the same layout for the
 *  watched elements — which is the condition those sleeps were approximating —
 *  and is bounded so a wedged page still fails on its assertion rather than
 *  hanging the suite.
 *
 *  It is deliberately NOT a drop-in for every wait: where a sleep encodes real
 *  product timing (a CSS show-delay, the 300ms filter debounce, an SSE
 *  reconnect), replacing it with this would let the assertion run before the
 *  behaviour under test has happened, and the scenario would pass against a
 *  broken UI. Those sleeps are left exactly as they are.
 */
export async function settled(page, { timeout = 2000 } = {}) {
  await page.evaluate(async (budget) => {
    const deadline = performance.now() + budget;
    const frame = () => new Promise((r) => requestAnimationFrame(r));
    const snap = () => {
      // Cheap whole-layout fingerprint: geometry of every element that occupies
      // space. If two frames agree on this, layout has stopped moving.
      let s = "";
      for (const el of document.querySelectorAll("body *")) {
        const r = el.getBoundingClientRect();
        if (r.width || r.height) s += `${r.x},${r.y},${r.width},${r.height};`;
      }
      return s;
    };
    let prev = snap();
    while (performance.now() < deadline) {
      await frame();
      const now = snap();
      if (now === prev) return;
      prev = now;
    }
  }, timeout);
}

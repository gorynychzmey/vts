import { chromium } from "playwright";
import http from "http";
import fs from "fs";
import path from "path";

export const STATIC_DIR = "/home/victor/dev/vts/vts/static";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
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

export async function openPage(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
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

const SHOT_DIR = "/tmp/vts-ui-verify";
export async function screenshot(page, name) {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const p = `${SHOT_DIR}/${name}.png`;
  await page.screenshot({ path: p });
  return p;
}

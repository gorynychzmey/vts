// The preset dialog and its tags (vts-qwcj).
//
// * GET /api/tags failed: the dialog's tag field cannot map the preset's ids
//   to names, so it shows none. Saving a rename must then send the preset's
//   stored ids unchanged — PATCH replaces `options` wholesale, so [] would
//   wipe the preset's tags.
// * A save the server refuses (422 with a detail) says so inside the dialog
//   instead of only in the console, and the dialog keeps the form.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR, openFromHeaderMenu } from "../harness.mjs";

export const name = "tags-preset-dialog";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

async function startServer() {
  const requests = [];
  const state = { refusePatch: false };
  const presets = [
    { source: "system", id: "default", name: "System", editable: false,
      options: { transcript: true, prompts: [], delivery: [], tags: [] } },
    { source: "user", id: "p", name: "Pea", editable: true,
      options: { transcript: true, prompts: [], delivery: [], tags: ["t-fam", "t-work"] } },
  ];
  const server = http.createServer((req, res) => {
    const p = new URL(req.url, "http://x").pathname;
    if (!p.startsWith("/api/")) {
      const f = p === "/" ? "/index.html" : p.replace("/static/", "/");
      const fp = path.join(STATIC_DIR, f);
      if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
      const ext = path.extname(fp);
      if (ext === ".woff2" || ext === ".woff" || ext === ".png") { res.end(fs.readFileSync(fp)); return; }
      res.setHeader("Content-Type", CT[ext] || "text/plain");
      res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
      return;
    }
    if (p === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      return;
    }
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      let body = null;
      try { body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "null"); } catch { body = null; }
      requests.push({ method: req.method, path: p, body });
      const json = (status, value) => {
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(value));
      };
      if (p === "/api/tags" && req.method === "GET") return json(500, { detail: "down" });
      if (p === "/api/presets/p" && req.method === "PATCH" && state.refusePatch) {
        return json(422, { detail: "Preset name is taken" });
      }
      if (req.method !== "GET") return json(200, { status: "ok", id: "p", source: "user" });
      const table = {
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/tasks": [],
        "/api/prompts": [],
        "/api/presets": presets,
        "/api/me/default_preset": { source: "system", id: "default" },
      };
      return json(200, p in table ? table[p] : {});
    });
  });
  await new Promise((r) => server.listen(0, r));
  return { state, server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests };
}

async function waitRequest(requests, from, pred, timeout = 4000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const hit = requests.slice(from).find(pred);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 25));
  }
  return null;
}

export async function run() {
  const failures = [];
  const { state, server, baseUrl, requests } = await startServer();
  const browser = await launch();
  try {
    const errors = [];
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      // The failing /api/tags and the refused PATCH are provoked on purpose.
      const text = m.text();
      if (m.type() === "error" && !text.includes("EventSource") && !text.includes("500 (")
          && !text.includes("422 (") && !text.includes("Failed to save preset")) {
        errors.push("console.error: " + text);
      }
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => !!document.getElementById("preset-pill-label")?.textContent, null, { timeout: 8000 }).catch(() => {});
    await openFromHeaderMenu(page, "#presets-btn");
    await page.click("#presets-list .mgr-item >> text=Pea", { timeout: 3000 }).catch(() => {});
    const nameInput = "#preset-name-input";
    await page.waitForFunction((s) => document.querySelector(s)?.value === "Pea", nameInput, { timeout: 3000 }).catch(() => {});
    await page.fill(nameInput, "Pea renamed");

    let from = requests.length;
    await page.click("#preset-submit-btn");
    const patch = await waitRequest(requests, from, (r) => r.method === "PATCH" && r.path === "/api/presets/p");
    if (!patch) failures.push("saving the preset sent no PATCH /api/presets/p");
    else {
      const sent = patch.body && patch.body.options && patch.body.options.tags;
      if (JSON.stringify(sent) !== JSON.stringify(["t-fam", "t-work"])) {
        failures.push(`with /api/tags down, renaming wrote options.tags = ${JSON.stringify(sent)}, expected the stored ["t-fam","t-work"]`);
      }
    }

    // A refused save explains itself in the dialog and keeps the form.
    await page.click("#presets-list .mgr-item >> text=Pea", { timeout: 3000 }).catch(() => {});
    await page.waitForFunction((s) => document.querySelector(s)?.value === "Pea", nameInput, { timeout: 3000 }).catch(() => {});
    state.refusePatch = true;
    await page.fill(nameInput, "Taken");
    await page.click("#preset-submit-btn");
    const shown = await page.waitForFunction(() => {
      const el = document.getElementById("preset-form-error");
      return el && !el.classList.contains("hidden") && el.offsetParent !== null ? el.textContent : false;
    }, null, { timeout: 3000 }).then((h) => h.jsonValue()).catch(() => null);
    if (shown !== "Preset name is taken") failures.push(`a refused preset save should show the server's reason in the dialog; got ${JSON.stringify(shown)}`);
    const kept = await page.inputValue(nameInput);
    if (kept !== "Taken") failures.push(`after a refused save the form should keep the typed name; got ${JSON.stringify(kept)}`);
    // The next attempt clears the message.
    state.refusePatch = false;
    await page.click("#preset-submit-btn");
    const cleared = await page.waitForFunction(() => {
      const el = document.getElementById("preset-form-error");
      return !el || el.classList.contains("hidden");
    }, null, { timeout: 3000 }).then(() => true).catch(() => false);
    if (!cleared) failures.push("the error should clear once the save goes through");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

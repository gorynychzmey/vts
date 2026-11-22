// Switching presets in the composer keeps the tag field honest (vts-qwcj).
//
// A preset fills the field with its tags. Choosing another preset must take
// the previous preset's contribution back out — otherwise "Save changes" on
// the new preset writes the old one's tags into it (PATCH replaces options
// wholesale, and the composer saves every chip). Tags the user typed by hand
// stay through a switch. And when GET /api/tags failed, the composer cannot
// map anything, so a save keeps the preset's stored tag ids instead of
// writing [].
//
//   default D (user) [Work], user preset P [Family]
//   load -> [Work]; pick P -> [Family]; change language; Save changes ->
//   PATCH /api/presets/p options.tags == ["t-fam"]; the reload leaves [Family];
//   type "Ручной", pick D -> [Ручной, Work]; pick P -> [Ручной, Family].
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR } from "../harness.mjs";

export const name = "tags-preset-switch";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

// `switchable`: GET /api/tags answers until `server.switchUser()`, after which
// it fails and preset P carries an id only the new user has — what a user
// switch (refreshAll) looks like when the new user's tag list cannot load.
async function startServer({ tagsFail, switchable = false }) {
  const requests = [];
  let switched = false;
  const tags = [
    { id: "t-work", name: "Work", count: 1 },
    { id: "t-fam", name: "Family", count: 1 },
  ];
  const presets = [
    { source: "system", id: "default", name: "System", editable: false,
      options: { transcript: true, prompts: [], delivery: [], tags: [] } },
    { source: "user", id: "d", name: "Dee", editable: true,
      options: { transcript: true, prompts: [], delivery: [], tags: ["t-work"] } },
    { source: "user", id: "p", name: "Pea", editable: true,
      options: { transcript: true, prompts: [], delivery: [], tags: ["t-fam"] } },
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
      if (p === "/api/tags" && req.method === "GET") return tagsFail || switched ? json(500, { detail: "down" }) : json(200, tags);
      if (p === "/api/presets" && switched) {
        return json(200, presets.map((x) => (x.id === "p" ? { ...x, options: { ...x.options, tags: ["t-other"] } } : x)));
      }
      if (req.method !== "GET") return json(200, { status: "ok" });
      const table = {
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/tasks": [],
        "/api/prompts": [],
        "/api/presets": presets,
        "/api/me/default_preset": { source: "user", id: "d" },
      };
      return json(200, p in table ? table[p] : {});
    });
  });
  await new Promise((r) => server.listen(0, r));
  return {
    server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests,
    switchUser: () => { if (switchable) switched = true; },
  };
}

const F = "#task-tags-field";
const chips = (page) => page.$$eval(`${F} .tag-chip .tag-chip-label`, (els) => els.map((e) => e.textContent));

async function waitChips(page, want) {
  return page.waitForFunction(([s, w]) => {
    const got = [...document.querySelectorAll(`${s} .tag-chip .tag-chip-label`)].map((e) => e.textContent);
    return JSON.stringify(got) === JSON.stringify(w);
  }, [F, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

async function pickPreset(page, label) {
  await page.click("#preset-pill");
  await page.click(`#preset-menu .pick-row >> text=${label}`, { timeout: 3000 }).catch(() => {});
}

async function saveChanges(page, requests) {
  const from = requests.length;
  await page.selectOption("#language", "de");
  await page.click("#preset-pill");
  await page.waitForFunction(() => document.getElementById("preset-save-btn")?.dataset.mode === "patch", null, { timeout: 3000 }).catch(() => {});
  await page.click("#preset-save-btn", { timeout: 3000 }).catch(() => {});
  const end = Date.now() + 4000;
  while (Date.now() < end) {
    const hit = requests.slice(from).find((r) => r.method === "PATCH" && r.path === "/api/presets/p");
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 25));
  }
  return null;
}

function collectErrors(page, errors) {
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    // The failing /api/tags in the second half is provoked on purpose.
    if (m.type() === "error" && !m.text().includes("EventSource") && !m.text().includes("500 (Internal Server Error)")) {
      errors.push("console.error: " + m.text());
    }
  });
}

export async function run() {
  const failures = [];
  const browser = await launch();
  const ok = await startServer({ tagsFail: false });
  const down = await startServer({ tagsFail: true });
  const swap = await startServer({ tagsFail: false, switchable: true });
  try {
    const errors = [];
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    collectErrors(page, errors);
    await page.goto(ok.baseUrl, { waitUntil: "domcontentloaded" });
    if (!(await waitChips(page, ["Work"]))) {
      failures.push(`the default preset D should fill the field with [Work]; chips: ${JSON.stringify(await chips(page))}`);
    }
    await pickPreset(page, "Pea");
    if (!(await waitChips(page, ["Family"]))) {
      failures.push(`switching to P should replace D's tags with P's; chips: ${JSON.stringify(await chips(page))}`);
    }
    const patch = await saveChanges(page, ok.requests);
    if (!patch) failures.push("\"Save changes\" sent no PATCH /api/presets/p");
    else {
      const sent = patch.body && patch.body.options && patch.body.options.tags;
      if (JSON.stringify(sent) !== JSON.stringify(["t-fam"])) {
        failures.push(`"Save changes" on P wrote options.tags = ${JSON.stringify(sent)}, expected P's ids only ["t-fam"]`);
      }
    }
    // The save reloads presets (which applies the default) and re-applies P.
    await page.waitForFunction(() => document.getElementById("preset-pill-label")?.textContent === "Pea", null, { timeout: 3000 }).catch(() => {});
    if (!(await waitChips(page, ["Family"]))) {
      failures.push(`after "Save changes" the field should hold P's tags only; chips: ${JSON.stringify(await chips(page))}`);
    }

    // A hand-typed tag survives switching.
    await page.click(`${F} .tag-field-input`);
    await page.type(`${F} .tag-field-input`, "Ручной");
    await page.keyboard.press("Enter");
    await pickPreset(page, "Dee");
    if (!(await waitChips(page, ["Ручной", "Work"]))) {
      failures.push(`switching to D should keep the hand-typed tag and swap P's for D's; chips: ${JSON.stringify(await chips(page))}`);
    }
    await pickPreset(page, "Pea");
    if (!(await waitChips(page, ["Ручной", "Family"]))) {
      failures.push(`switching back to P: chips ${JSON.stringify(await chips(page))}, expected ["Ручной","Family"]`);
    }

    // GET /api/tags failed: a save must keep P's stored ids, not write [].
    const page2 = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    collectErrors(page2, errors);
    await page2.goto(down.baseUrl, { waitUntil: "domcontentloaded" });
    await page2.waitForSelector(`${F} .tag-field-input`, { timeout: 8000 }).catch(() => {});
    await page2.waitForFunction(() => document.getElementById("preset-pill-label")?.textContent === "Dee", null, { timeout: 4000 }).catch(() => {});
    await pickPreset(page2, "Pea");
    const patch2 = await saveChanges(page2, down.requests);
    if (!patch2) failures.push("with /api/tags down, \"Save changes\" sent no PATCH");
    else {
      const sent = patch2.body && patch2.body.options && patch2.body.options.tags;
      if (JSON.stringify(sent) !== JSON.stringify(["t-fam"])) {
        failures.push(`with /api/tags down, "Save changes" wrote options.tags = ${JSON.stringify(sent)}, expected the stored ["t-fam"]`);
      }
    }

    // A user switch whose GET /api/tags fails must not reuse the previous
    // user's list: the new user's P carries "t-other", which the old list
    // does not know, so trusting it would write [] (vts-qwcj).
    const page3 = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    collectErrors(page3, errors);
    await page3.goto(swap.baseUrl, { waitUntil: "domcontentloaded" });
    await page3.waitForFunction(() => document.getElementById("preset-pill-label")?.textContent === "Dee", null, { timeout: 6000 }).catch(() => {});
    swap.switchUser();
    // refreshAll() is what an impersonation change runs.
    await page3.evaluate(() => refreshAll());
    await page3.waitForFunction(() => document.getElementById("preset-pill-label")?.textContent === "Dee", null, { timeout: 4000 }).catch(() => {});
    await pickPreset(page3, "Pea");
    const patch3 = await saveChanges(page3, swap.requests);
    if (!patch3) failures.push("after a user switch, \"Save changes\" sent no PATCH");
    else {
      const sent = patch3.body && patch3.body.options && patch3.body.options.tags;
      if (JSON.stringify(sent) !== JSON.stringify(["t-other"])) {
        failures.push(`after a user switch with /api/tags down, "Save changes" wrote options.tags = ${JSON.stringify(sent)}, expected the stored ["t-other"]`);
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    ok.server.close();
    down.server.close();
    swap.server.close();
  }
  return failures;
}

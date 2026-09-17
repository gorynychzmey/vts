// An empty Library and a Library that failed to load must not look the same
// (vts-0bey).
//
// The code says so explicitly — "A failed load must not read as 'you have
// nothing'" — and then nothing checked it. The two states are one element
// apart: `#library-empty-state` is shown either way, and only its TEXT says
// which happened. A user told "Nothing here yet" after a 500 concludes their
// recordings are gone.
//
// Part of the deliberate pass over the Library asked for in vts-0bey: the tab
// had been covered one scenario per defect Victor found by eye, so the states
// nobody had looked at were the ones with no coverage at all.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage, isVisible, STATIC_DIR } from "../harness.mjs";

export const name = "library-empty-and-failed-load";

const EMPTY_TEXT = "Nothing here yet.";
const FAILED_TEXT = "Could not load the library.";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
  ".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png",
};

const API = {
  "/api/version": { version: "verify" },
  "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
  "/api/push/config": { enabled: false },
  "/api/tasks": [],
  "/api/prompts": [],
  "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
};

/** Serves the real frontend; `recordingsMode` decides what the library gets. */
function startServer(recordingsMode) {
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/recordings") {
      if (recordingsMode === "error") {
        res.statusCode = 500;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ detail: "boom" }));
        return;
      }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ items: [], total: 0 }));
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in API ? API[url] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") {
      res.setHeader("Content-Type", CT[ext]);
      res.end(fs.readFileSync(fp));
      return;
    }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") body = body.replaceAll("__VTS_VERSION__", "verify");
    res.setHeader("Content-Type", CT[ext] || "text/plain");
    res.end(body);
  });
  return server;
}

async function openLibrary(browser, baseUrl) {
  const { page, errors } = await openPage(browser, baseUrl);
  // The tab is hidden by default, and measuring a hidden container measures
  // the container: open it before looking at anything inside.
  await page.waitForSelector("#main-tab-library", { timeout: 8000 });
  await page.click("#main-tab-library");
  await page.waitForTimeout(500);
  return { page, errors };
}

async function readState(page) {
  return page.evaluate(() => {
    const el = document.getElementById("library-empty-state");
    return {
      present: !!el,
      hiddenAttr: el ? el.hidden : null,
      text: el ? el.textContent.trim() : null,
      display: el ? getComputedStyle(el).display : null,
      cards: document.querySelectorAll("#library-list .task").length,
    };
  });
}

export async function run() {
  const failures = [];
  const browser = await launch();
  const servers = [];
  try {
    // ---------------------------------------------------- an empty library
    {
      const server = startServer("empty");
      servers.push(server);
      await new Promise((r) => server.listen(0, r));
      const baseUrl = `http://localhost:${server.address().port}`;
      const { page, errors } = await openLibrary(browser, baseUrl);
      const state = await readState(page);

      if (!state.present) {
        failures.push("empty: #library-empty-state is missing from the markup");
      } else {
        if (state.hiddenAttr || state.display === "none") {
          failures.push(
            `empty: the "nothing here" line is not shown for an empty library: ` +
            `${JSON.stringify(state)}`
          );
        }
        if (state.text !== EMPTY_TEXT) {
          failures.push(`empty: the line reads ${JSON.stringify(state.text)}, expected ${JSON.stringify(EMPTY_TEXT)}`);
        }
      }
      if (state.cards !== 0) failures.push(`empty: ${state.cards} cards rendered for an empty library`);
      if (errors.length) failures.push("empty: JS errors: " + JSON.stringify(errors));
      await page.close();
    }

    // ------------------------------------------------- a library that 500s
    {
      const server = startServer("error");
      servers.push(server);
      await new Promise((r) => server.listen(0, r));
      const baseUrl = `http://localhost:${server.address().port}`;
      const { page, errors } = await openLibrary(browser, baseUrl);
      const state = await readState(page);

      if (!state.present) {
        failures.push("failed: #library-empty-state is missing from the markup");
      } else {
        if (state.hiddenAttr || state.display === "none") {
          failures.push(
            `failed: nothing is said after a failed load — the library looks ` +
            `simply empty: ${JSON.stringify(state)}`
          );
        }
        if (state.text === EMPTY_TEXT) {
          failures.push(
            "failed: a 500 reads as \"Nothing here yet\" — the user is told " +
            "their recordings do not exist"
          );
        } else if (state.text !== FAILED_TEXT) {
          failures.push(
            `failed: expected ${JSON.stringify(FAILED_TEXT)}, got ${JSON.stringify(state.text)}`
          );
        }
      }
      // The browser logs the 500 itself; only other errors matter.
      const unexpected = errors.filter(
        (e) => !e.includes("the server responded with a status of 500"),
      );
      if (unexpected.length) failures.push("failed: JS errors: " + JSON.stringify(unexpected));
      await page.close();
    }
  } finally {
    await browser.close();
    for (const s of servers) {
      s.closeAllConnections?.();
      s.close();
    }
  }
  return failures;
}

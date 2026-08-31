// The library pages like the task list (vts-lib5).
//
// It used to fetch everything in one request with limit=200 — which silently
// truncated at the server's own 200-row cap and grew slower with every
// recording. Now it pages, and each view's sentinel speaks only for itself:
// a hidden list must not load pages nobody is looking at.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage } from "../harness.mjs";

export const name = "library-paging";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);
const TOTAL = 97;

function recordings() {
  return Array.from({ length: TOTAL }, (_, i) => ({
    id: `aaaaaaaa-0000-0000-0000-${String(i).padStart(12, "0")}`,
    source_task_id: null, title: `Recording ${i}`, title_is_custom: false,
    source_url: `file://rec${i}.m4a`, duration_sec: 600, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [],
    recorded_at: "2026-08-20T10:00:00Z",
    created_at: "2026-08-20T10:00:00Z", updated_at: "2026-08-20T11:00:00Z",
  }));
}

export async function run() {
  const failures = [];
  const recs = recordings();
  const requests = [];

  // A paging-aware stub: the shared harness returns a fixed body, which cannot
  // show whether the client asked for one page or all of them.
  const server = http.createServer((req, res) => {
    const [url, qs] = req.url.split("?");
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url === "/api/recordings") {
        const q = new URLSearchParams(qs || "");
        const limit = Number(q.get("limit") || 50);
        const offset = Number(q.get("offset") || 0);
        requests.push({ limit, offset });
        res.end(JSON.stringify({ items: recs.slice(offset, offset + limit), total: TOTAL }));
        return;
      }
      res.end(JSON.stringify(url === "/api/tasks" ? [] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC, f);
    if (!fp.startsWith(STATIC) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    const ct = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css"
      : ext === ".json" ? "application/json" : "text/html";
    res.setHeader("Content-Type", `${ct}; charset=utf-8`);
    res.end(fs.readFileSync(fp));
  });
  await new Promise((resolve) => server.listen(0, resolve));

  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, `http://127.0.0.1:${server.address().port}`);
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length > 0,
      null, { timeout: 8000 },
    ).catch(() => {});

    const first = await page.evaluate(
      () => document.querySelectorAll("#library-list .task").length);
    if (first === 0) {
      failures.push("the library rendered nothing");
      return failures;
    }
    if (first >= TOTAL) {
      failures.push(
        `the library loaded all ${first} recordings at once — at the server's ` +
        `200-row cap this silently truncates`
      );
    }
    if (requests.length && requests[0].limit > 50) {
      failures.push(`the first page asked for ${requests[0].limit} rows`);
    }

    // Scrolling brings in the next page.
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForFunction(
      (n) => document.querySelectorAll("#library-list .task").length > n,
      first, { timeout: 8000 },
    ).catch(() => {});
    const grown = await page.evaluate(
      () => document.querySelectorAll("#library-list .task").length);
    if (grown <= first) {
      failures.push(`scrolling loaded no further recordings (still ${grown})`);
    }

    // A hidden list must stay quiet: its sentinel is in a `hidden` container,
    // and firing there would load pages nobody is looking at.
    const before = requests.length;
    await page.click("#main-tab-tasks");
    await page.waitForTimeout(300);
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(700);
    if (requests.length > before) {
      failures.push(
        `the hidden library fetched ${requests.length - before} more pages`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

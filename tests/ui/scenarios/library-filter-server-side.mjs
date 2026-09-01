// The library filter must reach the SERVER, not just the loaded page (vts-jv2n).
//
// Paging cut the client-side filter's universe from 200 rows to 30 without
// saying so. A user with 40 recordings searched by name and was told nothing
// matched, because the row was on page 2. "Not among the ones you have" is
// indistinguishable, on screen, from "does not exist".
//
// The stub therefore filters like the real endpoint does: if the client still
// filters locally, the needle on page 3 is unreachable and this fails.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage } from "../harness.mjs";

export const name = "library-filter-server-side";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);
const TOTAL = 90;
// Deliberately far past the first page of 30.
const NEEDLE_AT = 71;

function recordings() {
  return Array.from({ length: TOTAL }, (_, i) => ({
    id: `bbbbbbbb-0000-0000-0000-${String(i).padStart(12, "0")}`,
    source_task_id: null,
    title: i === NEEDLE_AT ? "Совещание про бюджет" : `Recording ${i}`,
    title_is_custom: false,
    source_url: `file://rec${i}.m4a`, duration_sec: 600, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [],
    recorded_at: "2026-08-20T10:00:00Z",
    created_at: "2026-08-20T10:00:00Z", updated_at: "2026-08-20T11:00:00Z",
  }));
}

export async function run() {
  const failures = [];
  const all = recordings();
  const queries = [];

  const server = http.createServer((req, res) => {
    const [url, qs] = req.url.split("?");
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url === "/api/recordings") {
        const p = new URLSearchParams(qs || "");
        const limit = Number(p.get("limit") || 50);
        const offset = Number(p.get("offset") || 0);
        const q = (p.get("q") || "").toLowerCase();
        queries.push(q);
        // Filter server-side, exactly like the endpoint.
        const matched = q
          ? all.filter((r) => (r.title || "").toLowerCase().includes(q))
          : all;
        res.end(JSON.stringify({
          items: matched.slice(offset, offset + limit),
          total: matched.length,
        }));
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
    const { page } = await openPage(browser, `http://127.0.0.1:${server.address().port}`);
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length > 0,
      { timeout: 8000 },
    );

    await page.fill("#library-q", "бюджет");
    // Wait on the CONDITION, not a sleep: the filter is debounced and the
    // reload is async, so a fixed wait either flakes or wastes time.
    const found = await page.waitForFunction(
      () => {
        const cards = [...document.querySelectorAll("#library-list .task")];
        return cards.length === 1 && /бюджет/i.test(cards[0].textContent || "");
      },
      { timeout: 8000 },
    ).then(() => true).catch(() => false);

    if (!found) {
      const shown = await page.$$eval(
        "#library-list .task",
        (els) => els.map((e) => (e.textContent || "").trim().slice(0, 40)),
      );
      failures.push(
        `a recording on page 3 was not found by name; the list showed ${shown.length} card(s): ${JSON.stringify(shown)}`,
      );
    }

    // And the filter must actually have travelled to the server.
    if (!queries.some((q) => q.includes("бюджет"))) {
      failures.push(
        `the name filter never reached the server; q values seen: ${JSON.stringify(queries)}`,
      );
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

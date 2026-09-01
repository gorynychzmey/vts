// Switching tabs while a load is still IN FLIGHT (vts-0bey).
//
// Scope, stated precisely because I checked it: this does NOT reproduce
// 7497036 (recordings carrying the task's data-task-id). That bug needs the
// LIBRARY to load first — the order a reload on a remembered tab produces —
// so the recordings claim the ids before the task list renders.
// `library-does-not-shadow-tasks` covers exactly that, and still fails when
// the fix is reverted; verified rather than assumed.
//
// What is left uncovered, and is what this drives, are the INTERLEAVINGS:
// abandoning a load by switching away mid-flight, returning to a view whose
// response arrived while it was hidden, and a second load landing on top of
// the first. Those states nobody had exercised, and the counts are asserted
// exactly (=== 12, never "> 0") because a shared-key collision drops SOME
// cards, not all of them — a >0 check walks straight past it.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage } from "../harness.mjs";

export const name = "library-tab-switch-races";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);

function tasks(n) {
  return Array.from({ length: n }, (_, i) => ({
    id: `11111111-0000-0000-0000-${String(i).padStart(12, "0")}`,
    source_url: `https://example.com/t${i}`, source_title: `Task ${i}`,
    status: "completed", created_at: "2026-08-20T09:00:00Z",
    updated_at: "2026-08-20T09:30:00Z", steps: [], options: {},
    prompt_results: [], duration_sec: 300, language: "ru",
  }));
}

function recordings(n) {
  return Array.from({ length: n }, (_, i) => ({
    // Deliberately the SAME uuid tail as the tasks above: if either list keys
    // its DOM by a shared id again, the collision is guaranteed here.
    id: `11111111-0000-0000-0000-${String(i).padStart(12, "0")}`,
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
  let slowRecordings = false;

  const server = http.createServer(async (req, res) => {
    const [url, qs] = req.url.split("?");
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url === "/api/recordings") {
        const p = new URLSearchParams(qs || "");
        const limit = Number(p.get("limit") || 50);
        const offset = Number(p.get("offset") || 0);
        const all = recordings(12);
        if (slowRecordings) {
          // Long enough to still be in flight when the tab is switched away.
          await new Promise((r) => setTimeout(r, 900));
        }
        res.end(JSON.stringify({
          items: all.slice(offset, offset + limit), total: all.length,
        }));
        return;
      }
      if (url === "/api/tasks") {
        res.end(JSON.stringify(tasks(12)));
        return;
      }
      res.end(JSON.stringify({}));
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
  const countIn = (page, sel) =>
    page.$$eval(sel, (els) => els.length).catch(() => -1);

  try {
    const { page } = await openPage(browser, `http://127.0.0.1:${server.address().port}`);
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.waitForFunction(
      () => document.querySelectorAll("#task-list .task").length === 12,
      { timeout: 8000 },
    );

    // 1. Plain round trip: Library and back. Both lists must survive it.
    await page.click("#main-tab-library");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length > 0,
      { timeout: 8000 },
    );
    await page.click("#main-tab-tasks");
    // EXACTLY 12, not "more than none": a shared DOM key drops SOME cards
    // (the ones whose id a recording already claimed), so a >0 check passes
    // straight through the bug this scenario exists for.
    const afterRoundTrip = await page.waitForFunction(
      () => document.querySelectorAll("#task-list .task").length === 12,
      { timeout: 5000 },
    ).then(() => true).catch(() => false);
    if (!afterRoundTrip) {
      failures.push(
        `expected 12 task cards after a Library round trip, got ${await countIn(page, "#task-list .task")} — the two lists are sharing DOM keys again`,
      );
    }

    // 2. Switch away WHILE the library is still loading, then come back.
    slowRecordings = true;
    await page.click("#main-tab-library");
    await page.click("#main-tab-tasks");            // abandon mid-flight
    const tasksIntact = await page.waitForFunction(
      () => document.querySelectorAll("#task-list .task").length === 12,
      { timeout: 5000 },
    ).then(() => true).catch(() => false);
    if (!tasksIntact) {
      failures.push(
        `expected 12 task cards after abandoning a Library load, got ${await countIn(page, "#task-list .task")}`,
      );
    }

    // 3. Back to the Library: the abandoned response must not have left it
    //    stuck, half-rendered or duplicated.
    await page.click("#main-tab-library");
    const libraryRecovers = await page.waitForFunction(
      () => {
        const n = document.querySelectorAll("#library-list .task").length;
        return n === 12;    // fully rendered, and not doubled by two loads
      },
      { timeout: 8000 },
    ).then(() => true).catch(() => false);
    if (!libraryRecovers) {
      failures.push(
        `the library did not recover after an abandoned load (${await countIn(page, "#library-list .task")} cards; 12 expected)`,
      );
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

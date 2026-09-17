// Deleting the last card on screen must not strand the rows below it.
//
// Reported 2026-09-10 (vts-lism) as the open edge of 01e2f92: removeTask()
// stopped calling loadTasks() and now drops the one card, which is what keeps
// the pages below the first alive. When that card is the only one left in the
// DOM, updateHeadTail() reads an empty list and head/tail become null — and a
// null tail is what both #task-load-more (hidden) and loadNextPage() (returns
// at once) read as "there is no next page".
//
// The reachable path is NOT "delete a whole page", as first supposed. Measured
// 2026-09-17: the sentinel observer fires while the list shrinks, so deleting
// card after card normally pulls the next page in and the user never lands in
// the dead state. What does reach it is a page fetch that FAILED — the case the
// #task-load-more comment calls "the manual retry after a failed fetch". Once
// the list empties, that retry is hidden by the very condition it exists for,
// and infinite scroll is dead too: the rows are unreachable until something
// unrelated (an SSE reconnect, a filter change, F5) happens to rebuild the list.
//
// So the assertion is about what the USER can do, not about which variable
// holds the cursor: with rows still on the server, is there anything at all on
// screen that leads to them. Any fix that leaves a way through passes.
//
// Second, smaller edge in the same handler: renderTasks() ends with
// updateEmptyState() and removeTask() does not, so a filtered list emptied by
// deletion shows neither cards nor the "no matches" line — a blank area.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, clickReal, isVisible } from "../harness.mjs";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);

export const name = "task-delete-empties-list";

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  running:   { is_active:true,  is_pending:false, is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
  archived:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
};

// A page of three, not the product's twenty: the situation needs every card on
// screen deleted, and nothing in the path cares how many that is.
const PAGE = 3;
const TOTAL = 8;

function id(i) {
  return `a${String(i).padStart(7, "0")}-1111-1111-1111-111111111111`;
}

function task(i) {
  // Descending timestamps so the cursor is unambiguous, one second apart.
  const ts = new Date(Date.UTC(2026, 6, 30, 12, 0, 0) - i * 1000).toISOString()
    .replace("Z", "000+00:00");
  return {
    id: id(i), source_url: "http://x/" + i, source_title: `Task ${i}`, status: "completed",
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
    steps: [], capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: ts, updated_at: ts,
    progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } },
    stats: {},
  };
}

async function deleteCard(page, taskId) {
  page.once("dialog", (d) => d.accept());
  await clickReal(page, `[data-task-id="${taskId}"] .task-menu-btn`);
  await clickReal(page, `[data-task-id="${taskId}"] .delete-btn`);
  return page.waitForFunction(
    (tid) => !document.querySelector(`#task-list [data-task-id="${tid}"]`),
    taskId, { timeout: 8000 },
  ).then(() => true).catch(() => false);
}

const cardCount = (page) => page.$$eval("#task-list .task", (els) => els.length);

const hasCards = (page, timeout) => page.waitForFunction(
  () => document.querySelectorAll("#task-list .task").length > 0,
  null, { timeout },
).then(() => true).catch(() => false);

export async function run() {
  const failures = [];
  // Mutable server state: a stub that ignores the DELETE cannot show the bug.
  let rows = Array.from({ length: TOTAL }, (_, i) => task(i));
  const matching = (q) => (q ? rows.filter((t) => t.source_title.includes(q)) : rows);
  const streams = [];

  const server = http.createServer((req, res) => {
    const [rawPath, qs] = req.url.split("?");
    const q = new URLSearchParams(qs || "");
    if (rawPath.startsWith("/api/")) {
      if (rawPath === "/api/events") {
        // Held OPEN, like the real endpoint. A stub that answers JSON here makes
        // EventSource fail and reconnect every 2s, and every reconnect runs
        // resyncAfterReconnect() -> loadFirstPage() on an empty list. That
        // repairs the state under test: measured 2026-09-17, the list refilled
        // by itself mid-run and the scenario passed against the bug.
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        });
        res.write(": hello\n\n");
        streams.push(res);
        return;
      }
      res.setHeader("Content-Type", "application/json");

      if (rawPath === "/api/status-config") {
        res.end(JSON.stringify({ status_flags: FLAGS, tasks_page_size: PAGE }));
        return;
      }
      if (rawPath === "/api/tasks" && req.method === "GET") {
        const pool = matching(q.get("q"));
        const beforeId = q.get("before_id");
        if (beforeId) {
          // Every CURSOR fetch fails — one dropped connection while paging is
          // all it takes, and it is what puts the user one deletion away from
          // the dead state. First-page fetches (no cursor) still work, so a fix
          // that reloads the list is not being tested against a broken server.
          res.statusCode = 503;
          res.end(JSON.stringify({ detail: "the page fetch dropped" }));
          return;
        }
        res.end(JSON.stringify(pool.slice(0, PAGE)));
        return;
      }
      if (rawPath === "/api/tasks/count") {
        res.end(JSON.stringify({ count: matching(q.get("q")).length }));
        return;
      }
      if (rawPath === "/api/tasks" && req.method === "DELETE") {
        let body = "";
        req.on("data", (c) => { body += c; });
        req.on("end", () => {
          const ids = JSON.parse(body || "{}").task_ids || [];
          rows = rows.filter((t) => !ids.includes(t.id));
          res.end(JSON.stringify({ deleted: ids.length }));
        });
        return;
      }
      if (rawPath.startsWith("/api/tasks/") && req.method === "GET") {
        const wanted = rawPath.split("/").pop();
        res.end(JSON.stringify(rows.find((t) => t.id === wanted) || {}));
        return;
      }
      res.end(JSON.stringify(rawPath === "/api/tasks" ? [] : {}));
      return;
    }
    const f = rawPath === "/" ? "/index.html" : rawPath.replace("/static/", "/");
    const fp = path.join(STATIC, f);
    if (!fp.startsWith(STATIC) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") {
      res.end(fs.readFileSync(fp));
      return;
    }
    const ct = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css"
      : ext === ".json" ? "application/json" : "text/html";
    res.setHeader("Content-Type", `${ct}; charset=utf-8`);
    res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
  });
  await new Promise((r) => server.listen(0, r));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;

  const browser = await launch();
  try {
    // Taller than the harness default: the card kebab opens downwards, and the
    // menu of the last card on a short list must still be clickable.
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    // NOT openPage(): it waits for `networkidle`, which never arrives while the
    // event stream is held open. The first card is the readiness signal here.
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(`[data-task-id="${id(0)}"]`, { timeout: 8000 });
    // Let the sentinel observer take its shot and fail, so the list has settled
    // into the state the user acts on.
    await page.waitForTimeout(800);

    // --- SETUP GUARD -------------------------------------------------------
    // Everything below reasons about "every card on screen was deleted", so it
    // has to know how many that is. Without this the scenario would pass just
    // as happily on a list that never loaded.
    const firstPage = await cardCount(page);
    if (firstPage !== PAGE) {
      failures.push(
        `the list settled at ${firstPage} cards, expected ${PAGE}; the rest of this scenario proves nothing`,
      );
      return failures;
    }

    // --- A. EMPTY THE SCREEN WHILE THE SERVER STILL HAS ROWS ---------------
    for (let i = 0; i < PAGE; i += 1) {
      const gone = await deleteCard(page, id(i));
      if (!gone) {
        failures.push(`card ${i} is still on screen after deleting it; the rest proves nothing`);
        return failures;
      }
    }
    const left = TOTAL - PAGE;
    if (rows.length !== left) {
      failures.push(
        `the server holds ${rows.length} rows after ${PAGE} deletions, expected ${left}; the stub is wrong`,
      );
      return failures;
    }

    let reachable = await hasCards(page, 4000);
    let via = "the list came back on its own";
    if (!reachable) {
      // A fix may leave the rows one deliberate click away instead of loading
      // them, which is just as usable — so try the control the UI offers.
      if (await isVisible(page, "#task-load-more")) {
        await clickReal(page, "#task-load-more");
        reachable = await hasCards(page, 4000);
        via = reachable ? "'show more' was there and worked" : "'show more' was there and loaded nothing";
      } else {
        via = "the list is empty and there is no 'show more' to click";
      }
    }

    if (!reachable) {
      const count = await page.evaluate(
        () => (document.getElementById("tasks-count")?.textContent || "").trim(),
      );
      failures.push(
        `${left} tasks are still on the server and none of them can be reached from the screen`
        + ` — ${via} (the counter reads "${count}")`,
      );
    } else {
      // EXACT, not ">0": one page's worth is what comes back, and a list that
      // rebuilt into some other size is not the state being claimed.
      const after = await cardCount(page);
      const want = Math.min(left, PAGE);
      if (after !== want) {
        failures.push(`the list came back with ${after} cards, expected ${want} (${via})`);
      }
    }

    // --- B. A FILTERED LIST EMPTIED BY DELETION SAYS SO --------------------
    // "Task 7" is the title of exactly one row, so deleting it leaves the filter
    // matching nothing: the "no matches" line is the only thing left to say.
    await page.fill("#filter-q", "Task 7");
    const filtered = await page.waitForFunction(
      () => document.querySelectorAll("#task-list .task").length === 1,
      null, { timeout: 8000 },
    ).then(() => true).catch(() => false);
    if (!filtered) {
      failures.push(
        `the filter left ${await cardCount(page)} cards, expected 1; the empty-state check proves nothing`,
      );
      return failures;
    }
    if (!await deleteCard(page, id(7))) {
      failures.push("the filtered card is still on screen after deleting it");
      return failures;
    }
    const emptyShown = await page.waitForFunction(
      () => {
        const el = document.getElementById("task-empty");
        return !!el && !el.hidden;
      },
      null, { timeout: 4000 },
    ).then(() => true).catch(() => false);
    if (!emptyShown) {
      failures.push(
        "a filtered list emptied by deletion shows no cards and no 'no matches' line —"
        + " the user is left looking at a blank area",
      );
    }
  } finally {
    await browser.close();
    streams.forEach((res) => res.end());
    server.closeAllConnections?.();
    server.close();
  }
  return failures;
}

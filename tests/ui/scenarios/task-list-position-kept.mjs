// Archiving or deleting a task must not throw away the pages below the first.
//
// Reported 2026-09-10: standing somewhere in the SECOND twenty, archive or
// delete a task — the list reloads and the card you were looking at is no
// longer loaded at all, because both handlers called loadTasks(), which is
// loadFirstPage(): it blanks taskList and re-fetches ONE page.
//
// Archiving is the clearer of the two: the task stays in the list (there is no
// default status filter, and prod carries 6 archived rows among 123), so the
// only thing that has to change is one card's status. Rebuilding the list to
// repaint one badge is what loses the position.
//
// Deletion genuinely removes a row, so the neighbours have to survive instead:
// drop the card, keep the rest.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage, clickReal } from "../harness.mjs";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);

export const name = "task-list-position-kept";

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  running:   { is_active:true,  is_pending:false, is_finished:false, shows_progress:true,  can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
  archived:  { is_active:false, is_pending:false, is_finished:true,  shows_progress:false, can_pause:false, can_resume:false, can_archive:false },
};

const PAGE = 20;
const TOTAL = 45;

function id(i) {
  return `a${String(i).padStart(7, "0")}-1111-1111-1111-111111111111`;
}

function task(i, status = "completed") {
  // Descending timestamps so the cursor is unambiguous, one second apart.
  const ts = new Date(Date.UTC(2026, 6, 30, 12, 0, 0) - i * 1000).toISOString()
    .replace("Z", "000+00:00");
  return {
    id: id(i), source_url: "http://x/" + i, source_title: `Task ${i}`, status,
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
    steps: [], capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: ts, updated_at: ts,
    progress: { transcribe: { current: 0, total: 0 }, summary: { current: 0, total: 0 } },
    stats: {},
  };
}

export async function run() {
  const failures = [];
  // Mutable server state so archive/delete actually change what /api/tasks
  // returns — a stub that ignores the write cannot show the bug.
  let rows = Array.from({ length: TOTAL }, (_, i) => task(i));

  const server = http.createServer((req, res) => {
    const [rawPath, qs] = req.url.split("?");
    const q = new URLSearchParams(qs || "");
    if (rawPath.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");

      if (rawPath === "/api/status-config") {
        res.end(JSON.stringify({ status_flags: FLAGS, tasks_page_size: PAGE }));
        return;
      }
      if (rawPath === "/api/tasks" && req.method === "GET") {
        // Cursor paging, the same shape the server implements: everything
        // strictly after the row named by before_id.
        const beforeId = q.get("before_id");
        let slice = rows;
        if (beforeId) {
          const at = rows.findIndex((t) => t.id === beforeId);
          slice = at >= 0 ? rows.slice(at + 1) : [];
        }
        res.end(JSON.stringify(slice.slice(0, PAGE)));
        return;
      }
      if (rawPath === "/api/tasks/count") {
        res.end(JSON.stringify({ count: rows.length }));
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
      if (rawPath === "/api/tasks/archive") {
        let body = "";
        req.on("data", (c) => { body += c; });
        req.on("end", () => {
          const ids = JSON.parse(body || "{}").task_ids || [];
          rows = rows.map((t) => (ids.includes(t.id) ? { ...t, status: "archived" } : t));
          res.end(JSON.stringify({ archived: ids.length }));
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
    const { page } = await openPage(browser, baseUrl);
    await page.waitForSelector(`[data-task-id="${id(0)}"]`, { timeout: 8000 });

    // Load the second page the way the user does.
    await page.evaluate(() => document.getElementById("task-sentinel")
      ?.scrollIntoView({ block: "end" }));
    const loadedTwoPages = await page.waitForFunction(
      (n) => document.querySelectorAll("#task-list .task").length > n,
      PAGE, { timeout: 8000 },
    ).then(() => true).catch(() => false);
    if (!loadedTwoPages) {
      failures.push("the second page never loaded; the rest of the scenario cannot run");
      return failures;
    }

    const target = id(25);       // squarely in the second twenty
    const neighbour = id(26);
    const before = await page.$$eval("#task-list .task", (els) => els.length);
    // Scroll to the target so the situation matches the report: the user is
    // LOOKING at a card in the second twenty, not sitting at the top.
    await page.evaluate((tid) => document
      .querySelector(`[data-task-id="${tid}"]`)?.scrollIntoView({ block: "center" }), target);
    const scrollBefore = await page.evaluate(() => Math.round(window.scrollY));
    const topBefore = await page.evaluate((tid) => Math.round(
      document.querySelector(`[data-task-id="${tid}"]`).getBoundingClientRect().top), target);

    // --- ARCHIVE -----------------------------------------------------------
    // Both actions live in the card's kebab menu, so it has to be opened first.
    page.once("dialog", (d) => d.accept());
    await clickReal(page, `[data-task-id="${target}"] .task-menu-btn`);
    await clickReal(page, `[data-task-id="${target}"] .archive-btn`);

    const archived = await page.waitForFunction(
      (tid) => {
        const el = document.querySelector(`#task-list [data-task-id="${tid}"]`);
        return el && /архив|archiv/i.test(el.querySelector(".task-status")?.textContent || "");
      },
      target, { timeout: 8000 },
    ).then(() => true).catch(() => false);

    const afterArchive = await page.$$eval("#task-list .task", (els) => els.length);
    const scrollAfter = await page.evaluate(() => Math.round(window.scrollY));
    if (!archived) {
      failures.push(
        `after archiving, the card is gone or still unarchived (list has ${afterArchive} cards, had ${before})`,
      );
    }
    // EXACT count, not ">= something": a rebuild leaves the first page intact,
    // so a loose check passes straight through the bug. The list must not
    // change size at all — archiving moves no row.
    if (afterArchive !== before) {
      failures.push(
        `archiving changed the list from ${before} to ${afterArchive} cards; it should change none`,
      );
    }
    // And the card must still sit where it was: a rebuild that happened to
    // re-fetch the same rows would satisfy a count check alone.
    const posAfterArchive = await page.$$eval(
      "#task-list .task", (els, tid) => els.findIndex((e) => e.dataset.taskId === tid), target,
    );
    if (posAfterArchive !== 25) {
      failures.push(
        `the archived card moved to index ${posAfterArchive}, expected 25 — the list was rebuilt`,
      );
    }
    if (Math.abs(scrollAfter - scrollBefore) > 4) {
      failures.push(
        `the viewport jumped from ${scrollBefore} to ${scrollAfter} while archiving`,
      );
    }

    // --- DELETE ------------------------------------------------------------
    page.once("dialog", (d) => d.accept());
    await clickReal(page, `[data-task-id="${id(30)}"] .task-menu-btn`);
    await clickReal(page, `[data-task-id="${id(30)}"] .delete-btn`);

    const deleted = await page.waitForFunction(
      (tid) => !document.querySelector(`#task-list [data-task-id="${tid}"]`),
      id(30), { timeout: 8000 },
    ).then(() => true).catch(() => false);
    if (!deleted) failures.push("the deleted card is still on screen");

    const neighbourAlive = await page.$(`#task-list [data-task-id="${neighbour}"]`);
    if (!neighbourAlive) {
      failures.push(
        "deleting dropped the neighbourhood: a card from the second page is no longer loaded",
      );
    }
    const afterDelete = await page.$$eval("#task-list .task", (els) => els.length);
    // EXACT, for the same reason the archive branch above spells out. `<` is
    // one-sided: it catches a rebuild that leaves FEWER cards (20 instead of
    // 39) but passes through anything that leaves more — a deletion that also
    // pulled a page in would satisfy it while the list is no longer the list
    // the user was looking at.
    if (afterDelete !== afterArchive - 1) {
      failures.push(
        `deleting left ${afterDelete} cards, expected ${afterArchive - 1} — the list was rebuilt`,
      );
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

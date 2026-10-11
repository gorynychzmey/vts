// The library must reflect a deletion and an impersonation switch, as the task
// list already does (reported 2026-10-11).
//
// Two separate gaps, one symptom — the library showing what is no longer so:
//
//  * Delete. A library card calls the same removeTask() as a task card, but it
//    carries no data-task-id on purpose (a recording is not a task), so
//    removeTask's findTaskEl() never reached it. The card stayed until a
//    manual refresh.
//  * User switch. refreshAll() is the impersonation path and reloads tasks,
//    prompts and presets — but the library was loaded once, lazily, behind a
//    `libraryLoaded` flag that nothing reset. Switching user kept showing the
//    previous user's recordings.
//
// Plus the guard that keeps the delete fix honest: the endpoint answers 200
// with "not_found" for an id it did not delete, and a card removed on THAT
// would misreport the user's data. It must stay.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, clickReal } from "../harness.mjs";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static");

export const name = "library-follows-delete-and-user-switch";

function rec(id, taskId, title) {
  return {
    id, source_task_id: taskId, title, source_url: `http://x/${id}`,
    created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z",
    duration_sec: 600, language: "ru",
    has_transcript: true, has_summary: false, has_redacted: false, prompt_results: [],
  };
}

export async function run() {
  const failures = [];
  // Per-user server state, keyed by as_user (empty = the admin themself).
  const libraries = {
    "": [
      rec("r-keep", "t-keep", "Admin keeps"),
      rec("r-gone", "t-gone", "Admin deletes"),
      rec("r-stuck", "t-stuck", "Server refuses"),
    ],
    "diana@schlachtenja.de": [rec("r-diana", "t-diana", "Diana only")],
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const asUser = url.searchParams.get("as_user") || "";
    if (url.pathname.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url.pathname === "/api/recordings") {
        const items = libraries[asUser] || [];
        res.end(JSON.stringify({ items, total: items.length }));
        return;
      }
      if (url.pathname === "/api/tasks" && req.method === "DELETE") {
        let body = "";
        req.on("data", (c) => { body += c; });
        req.on("end", () => {
          const ids = JSON.parse(body || "{}").task_ids || [];
          const results = {};
          for (const id of ids) {
            if (id === "t-stuck") { results[id] = "not_found"; continue; }
            libraries[asUser] = (libraries[asUser] || []).filter((r) => r.source_task_id !== id);
            results[id] = "deleted";
          }
          res.end(JSON.stringify({ results }));
        });
        return;
      }
      const table = {
        "/api/me": { requested_by: "admin", acting_as: asUser || "admin", is_admin: true },
        "/api/admin/users": { users: ["admin", "diana@schlachtenja.de"] },
        "/api/status-config": { status_flags: {} },
        "/api/tasks": [],
        "/api/version": { version: "verify" },
        "/api/push/config": { enabled: false },
        "/api/prompts": [],
      };
      res.end(JSON.stringify(url.pathname in table ? table[url.pathname] : {}));
      return;
    }
    const f = url.pathname === "/" ? "/index.html" : url.pathname.replace("/static/", "/");
    const fp = path.join(STATIC, f);
    if (!fp.startsWith(STATIC) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") { res.end(fs.readFileSync(fp)); return; }
    const ct = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css" : "text/html";
    res.setHeader("Content-Type", `${ct}; charset=utf-8`);
    res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
  });
  await new Promise((r) => server.listen(0, r));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const browser = await launch();

  const titles = (page) => page.$$eval("#library-list [data-recording-id]", (els) =>
    els.map((e) => e.dataset.recordingId).sort());

  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    page.on("dialog", (d) => d.accept());
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".main-tab[data-view=\"library\"]", { timeout: 8000 });
    await clickReal(page, ".main-tab[data-view=\"library\"]");

    // --- SETUP GUARD ------------------------------------------------------
    const loaded = await page.waitForFunction(
      () => document.querySelectorAll("#library-list [data-recording-id]").length === 3,
      null, { timeout: 5000 },
    ).then(() => true).catch(() => false);
    if (!loaded) {
      failures.push(`the library never showed the admin's 3 recordings (saw ${JSON.stringify(await titles(page))})`);
      return failures;
    }

    const deleteFromLibrary = async (recId) => {
      const card = `#library-list [data-recording-id="${recId}"]`;
      await clickReal(page, `${card} .task-menu-btn`);
      await page.waitForTimeout(150);
      await clickReal(page, `${card} .delete-btn`);
      await page.waitForTimeout(500);
    };

    // --- A. DELETED -> GONE FROM THE LIBRARY ------------------------------
    await deleteFromLibrary("r-gone");
    const afterDelete = await titles(page);
    if (JSON.stringify(afterDelete) !== JSON.stringify(["r-keep", "r-stuck"])) {
      failures.push(`after deleting from the library it shows ${JSON.stringify(afterDelete)}, expected ["r-keep","r-stuck"]`);
    }
    const count = await page.$eval("#library-count", (e) => e.textContent.trim()).catch(() => "");
    if (count !== "2") failures.push(`the library count reads ${JSON.stringify(count)} after a deletion, expected "2"`);

    // --- B. NOT DELETED -> STAYS ------------------------------------------
    await deleteFromLibrary("r-stuck");
    if (!(await titles(page)).includes("r-stuck")) {
      failures.push("a recording the server did NOT delete (not_found) vanished from the library anyway");
    }

    // --- C. USER SWITCH -> THE OTHER USER'S LIBRARY -----------------------
    await page.selectOption("#admin-user-select", "diana@schlachtenja.de");
    await clickReal(page, "#admin-apply-btn");
    const switched = await page.waitForFunction(
      () => {
        const ids = [...document.querySelectorAll("#library-list [data-recording-id]")].map((e) => e.dataset.recordingId);
        return ids.length === 1 && ids[0] === "r-diana";
      },
      null, { timeout: 5000 },
    ).then(() => true).catch(() => false);
    if (!switched) {
      failures.push(`after switching to diana the library shows ${JSON.stringify(await titles(page))}, expected ["r-diana"]`);
    }
  } finally {
    await browser.close();
    server.closeAllConnections?.();
    server.close();
  }
  return failures;
}

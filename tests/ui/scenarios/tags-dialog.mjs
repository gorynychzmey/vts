// The "Manage tags" dialog (vts-qwcj).
//
// Opened from the header menu (#tags-btn). Rows read "name · count"; an unused
// tag shows no number and is muted (`tag-row--unused`). Rename is inline:
// Enter saves, Esc cancels the rename without closing the dialog, and a 409
// shows "already exists" next to the input instead of failing silently.
// Delete asks first, naming how many recordings lose the tag. A field at the
// top creates a tag.
//
// A change reaches what is on screen without a reload: a renamed tag's chips
// on the task and library cards take the new name, a deleted tag's chips go,
// and a deleted tag is dropped from an active filter (which re-queries the
// list without it).
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR } from "../harness.mjs";

export const name = "tags-dialog";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const T1 = "abababab-8888-0000-0000-000000000001";
const T2 = "abababab-8888-0000-0000-000000000002";
const R1 = "cdcdcdcd-8888-0000-0000-000000000001";
const LONG = "Очень длинное имя тега, которое должно переноситься внутри чипа 12".slice(0, 64);

function makeState() {
  return {
    tags: [
      { id: "t-fam", name: "Family" },
      { id: "t-old", name: "Old" },
      { id: "t-work", name: "Work" },
      { id: "t-long", name: LONG },
    ],
    // tag ids per recording; T1 <-> R1 share one, T2 has its own
    links: { [R1]: ["t-fam", "t-work"], r2: ["t-work"] },
    made: 0,
  };
}

function startServer() {
  const st = makeState();
  const requests = [];
  const tagsFor = (rec) => st.links[rec]
    .map((id) => st.tags.find((t) => t.id === id)).filter(Boolean)
    .map(({ id, name }) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const count = (id) => Object.values(st.links).filter((ids) => ids.includes(id)).length;
  const keyTaken = (name, except) => st.tags.some((t) => t.id !== except && t.name.toLowerCase() === name.trim().toLowerCase());
  const task = (id, rec, title, n) => ({
    id, source_url: `https://example.com/${n}`, source_title: title,
    status: "completed", awaiting_step: null, queue: null, queue_position: null,
    transcript_path: "present", summary_path: null, redacted_path: null, media_path: null,
    options: { transcript: true, prompts: [], prompt_results: [] }, steps: [], capabilities: {},
    created_at: `2026-11-22T10:00:0${n}.000000`, updated_at: `2026-11-22T10:00:0${n}.000000`,
    progress: {}, stats: {}, tags: tagsFor(rec),
  });
  const recording = () => ({
    id: R1, source_task_id: T1, title: "Rec one", title_is_custom: false,
    source_url: "https://example.com/1", duration_sec: 60, language: "ru", tags: tagsFor(R1),
    has_transcript: true, has_redacted: false, has_summary: false, has_media: false,
    prompt_results: [], state: "complete",
    recorded_at: "2026-11-22T10:00:01Z", listed_at: "2026-11-22T10:00:01Z",
    created_at: "2026-11-22T10:00:01Z", updated_at: "2026-11-22T10:00:01Z",
  });
  const match = (row, sp) => {
    const want = sp.getAll("tag").map((s) => s.toLowerCase());
    if (!want.length) return true;
    const have = new Set(row.tags.map((t) => t.name.toLowerCase()));
    return sp.get("tag_match") === "any" ? want.some((w) => have.has(w)) : want.every((w) => have.has(w));
  };
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const p = url.pathname;
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
      requests.push({ method: req.method, path: p, params: url.searchParams, body });
      const json = (status, value) => {
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(value));
      };
      const sp = url.searchParams;
      const one = p.match(/^\/api\/tags\/([^/]+)$/);
      if (one && req.method === "PATCH") {
        const tag = st.tags.find((t) => t.id === one[1]);
        if (!tag) return json(404, { detail: "Tag not found" });
        if (keyTaken(body.name, tag.id)) return json(409, { detail: "Tag already exists" });
        tag.name = body.name.trim();
        st.tags.sort((a, b) => a.name.localeCompare(b.name));
        return json(200, { id: tag.id, name: tag.name });
      }
      if (one && req.method === "DELETE") {
        const n = count(one[1]);
        st.tags = st.tags.filter((t) => t.id !== one[1]);
        for (const k of Object.keys(st.links)) st.links[k] = st.links[k].filter((id) => id !== one[1]);
        return json(200, { unlinked: n });
      }
      if (p === "/api/tags" && req.method === "POST") {
        if (keyTaken(body.name)) return json(409, { detail: "Tag already exists" });
        const tag = { id: `t-made-${st.made++}`, name: body.name.trim() };
        st.tags.push(tag);
        st.tags.sort((a, b) => a.name.localeCompare(b.name));
        return json(201, tag);
      }
      if (req.method !== "GET") return json(200, { status: "ok" });
      if (p === "/api/tags") return json(200, st.tags.map((t) => ({ ...t, count: count(t.id) })));
      if (p === "/api/tasks") {
        return json(200, [task(T2, "r2", "Task two", 2), task(T1, R1, "Task one", 1)].filter((t) => match(t, sp)));
      }
      if (p === "/api/tasks/count") {
        return json(200, { total: [task(T2, "r2", "Task two", 2), task(T1, R1, "Task one", 1)].filter((t) => match(t, sp)).length });
      }
      if (p === "/api/recordings") {
        const items = [recording()].filter((r) => match(r, sp));
        return json(200, { items, total: items.length });
      }
      const table = {
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/prompts": [],
        "/api/presets": [],
      };
      return json(200, p in table ? table[p] : {});
    });
  });
  return new Promise((r) => server.listen(0, () => r({
    server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests,
  })));
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

const D = "#tags-dialog";
const rowSel = (id) => `${D} .tag-row[data-tag-id="${id}"]`;

const rows = (page) => page.evaluate((d) => [...document.querySelectorAll(`${d} .tag-row`)].map((r) => {
  const c = r.querySelector(".tag-row-count");
  return {
    id: r.dataset.tagId,
    name: r.querySelector(".tag-row-name")?.textContent || "",
    count: c && c.offsetParent !== null && !c.classList.contains("hidden") ? c.textContent.trim() : "",
    unused: r.classList.contains("tag-row--unused"),
  };
}), D);

async function waitRows(page, pred, timeout = 4000) {
  const end = Date.now() + timeout;
  let got = [];
  while (Date.now() < end) {
    got = await rows(page);
    if (pred(got)) return { ok: true, got };
    await page.waitForTimeout(40);
  }
  return { ok: false, got };
}

const cardChips = (page, sel) => page.evaluate((s) =>
  [...document.querySelectorAll(`${s} .task-tags .tag-chip-label`)].map((e) => e.textContent), sel);

async function waitChips(page, sel, want) {
  return page.waitForFunction(([s, w]) => {
    const got = [...document.querySelectorAll(`${s} .task-tags .tag-chip-label`)].map((e) => e.textContent);
    return JSON.stringify(got) === JSON.stringify(w);
  }, [sel, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

async function openDialog(page) {
  await page.click("#header-menu-btn");
  const item = await page.waitForSelector("#tags-btn", { state: "visible", timeout: 3000 }).then(() => true).catch(() => false);
  if (!item) return false;
  await page.click("#tags-btn");
  return page.waitForFunction((d) => document.querySelector(d)?.open === true, D, { timeout: 3000 })
    .then(() => true).catch(() => false);
}

async function startRename(page, id, value) {
  await page.click(`${rowSel(id)} .tag-row-rename`, { timeout: 2000 }).catch(() => {});
  const input = await page.waitForSelector(`${rowSel(id)} .tag-row-input`, { state: "visible", timeout: 2000 })
    .then(() => true).catch(() => false);
  if (!input) return false;
  await page.fill(`${rowSel(id)} .tag-row-input`, value);
  return true;
}

export async function run() {
  const failures = [];
  const { server, baseUrl, requests } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      // The 409s below are provoked on purpose.
      if (m.type() === "error" && !m.text().includes("EventSource") && !m.text().includes("409 (Conflict)")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const T1C = `#task-list [data-task-id="${T1}"]`;
    const T2C = `#task-list [data-task-id="${T2}"]`;
    const R1C = `#library-list [data-recording-id="${R1}"]`;
    const ready = await page.waitForSelector(T2C, { timeout: 8000 }).then(() => true).catch(() => false);
    if (!ready) {
      failures.push("the task list never rendered — nothing below proves anything");
      return failures;
    }
    // The library cards are loaded too, so the repaint can be seen in both lists.
    await page.click("#main-tab-library");
    await page.waitForSelector(R1C, { timeout: 8000 }).catch(() => {});
    await page.click("#main-tab-tasks");
    // A filter on Family, to see the delete drop it.
    if (await page.$("#filter-tags .tag-field-input")) {
      const from = requests.length;
      await page.click("#filter-tags .tag-field-input");
      await page.fill("#filter-tags .tag-field-input", "Family");
      await page.keyboard.press("Enter");
      await waitRequest(requests, from, (r) => r.path === "/api/tasks" && r.params.getAll("tag").includes("Family"));
      await page.waitForSelector(T2C, { state: "detached", timeout: 3000 }).catch(() => {});
    } else {
      failures.push("no tag picker in the task filters (#filter-tags)");
    }

    if (!(await openDialog(page))) {
      failures.push("\"Manage tags\" (#tags-btn) in the header menu did not open #tags-dialog");
      return failures;
    }
    const menuLabel = await page.evaluate(() => document.querySelector("#tags-btn [data-i18n]")?.dataset.i18n);
    if (menuLabel !== "tags.manage.open") failures.push(`the menu item's label should be tags.manage.open in an inner span; got ${menuLabel}`);

    // ---- rows: name · count, unused muted ----
    const first = await waitRows(page, (g) => g.length === 4);
    const byId = Object.fromEntries(first.got.map((r) => [r.id, r]));
    if (!first.ok) failures.push(`the dialog should list 4 tags; got ${JSON.stringify(first.got)}`);
    else {
      if (byId["t-work"].count !== "2" || byId["t-fam"].count !== "1") {
        failures.push(`counts should read Work 2, Family 1; got ${JSON.stringify(first.got)}`);
      }
      if (byId["t-old"].count !== "" || !byId["t-old"].unused) {
        failures.push(`an unused tag shows no number and is muted; got ${JSON.stringify(byId["t-old"])}`);
      }
      if (byId["t-work"].unused) failures.push("a used tag must not be muted");
      const muted = await page.evaluate(([a, b]) => {
        const c = (s) => getComputedStyle(document.querySelector(`${s} .tag-row-name`)).color;
        return c(a) !== c(b);
      }, [rowSel("t-old"), rowSel("t-work")]);
      if (!muted) failures.push("an unused tag should be shown in a different colour");
    }

    // ---- Esc cancels the rename, not the dialog ----
    if (await startRename(page, "t-work", "Whatever")) {
      await page.keyboard.press("Escape");
      const state = await page.evaluate(([d, r]) => ({
        open: document.querySelector(d)?.open === true,
        input: !!document.querySelector(`${r} .tag-row-input`),
        name: document.querySelector(`${r} .tag-row-name`)?.textContent,
      }), [D, rowSel("t-work")]);
      if (!state.open) failures.push("Esc in the rename input closed the whole dialog");
      if (state.input || state.name !== "Work") failures.push(`Esc should cancel the rename; got ${JSON.stringify(state)}`);
    } else {
      failures.push("the rename button did not open an inline input");
    }

    // ---- rename collision: inline error ----
    let from = requests.length;
    if (await startRename(page, "t-work", "family")) {
      await page.keyboard.press("Enter");
      const patch = await waitRequest(requests, from, (r) => r.method === "PATCH" && r.path === "/api/tags/t-work");
      if (!patch || patch.body?.name !== "family") failures.push(`Enter should PATCH the new name; sent ${JSON.stringify(patch && patch.body)}`);
      const want = await page.evaluate(() => t("tags.exists"));
      const shown = await page.waitForFunction(([r, w]) => {
        const el = document.querySelector(`${r} .tag-row-error`);
        return !!el && el.offsetParent !== null && el.textContent.trim() === w;
      }, [rowSel("t-work"), want], { timeout: 3000 }).then(() => true).catch(() => false);
      if (!shown || want === "tags.exists") failures.push(`a 409 should show "${want}" next to the input`);
      const stillEditing = await page.$(`${rowSel("t-work")} .tag-row-input`);
      if (!stillEditing) failures.push("after a 409 the input should stay open for another try");
    }

    // ---- rename: chips follow on both lists ----
    from = requests.length;
    await page.fill(`${rowSel("t-work")} .tag-row-input`, "Job").catch(() => {});
    await page.keyboard.press("Enter");
    const renamed = await waitRequest(requests, from, (r) => r.method === "PATCH" && r.body?.name === "Job");
    if (!renamed) failures.push("renaming to Job sent no PATCH");
    const afterRename = await waitRows(page, (g) => g.some((r) => r.id === "t-work" && r.name === "Job"));
    if (!afterRename.ok) failures.push(`the row should read Job after the rename; rows ${JSON.stringify(afterRename.got)}`);
    if (!(await waitChips(page, T1C, ["Family", "Job"]))) {
      failures.push(`the task card's chips should follow the rename; got ${JSON.stringify(await cardChips(page, T1C))}`);
    }
    if (!(await waitChips(page, R1C, ["Family", "Job"]))) {
      failures.push(`the library card's chips should follow the rename; got ${JSON.stringify(await cardChips(page, R1C))}`);
    }

    // ---- delete: confirm names the count; chips go; filter drops it ----
    from = requests.length;
    let message = null;
    page.once("dialog", (d) => { message = d.message(); void d.accept(); });
    await page.click(`${rowSel("t-fam")} .tag-row-delete`, { timeout: 2000 }).catch(() => {});
    const del = await waitRequest(requests, from, (r) => r.method === "DELETE" && r.path === "/api/tags/t-fam");
    const wantMsg = await page.evaluate(() => t("tags.delete.confirm", { name: "Family", count: 1 }));
    if (message !== wantMsg || wantMsg === "tags.delete.confirm") {
      failures.push(`the delete confirmation should read "${wantMsg}"; got ${JSON.stringify(message)}`);
    }
    if (!del) failures.push("confirming the delete sent no DELETE /api/tags/t-fam");
    const afterDelete = await waitRows(page, (g) => !g.some((r) => r.id === "t-fam"));
    if (!afterDelete.ok) failures.push(`the deleted tag should leave the dialog; rows ${JSON.stringify(afterDelete.got)}`);
    if (!(await waitChips(page, T1C, ["Job"]))) {
      failures.push(`the deleted tag's chip should leave the task card; got ${JSON.stringify(await cardChips(page, T1C))}`);
    }
    if (!(await waitChips(page, R1C, ["Job"]))) {
      failures.push(`the deleted tag's chip should leave the library card; got ${JSON.stringify(await cardChips(page, R1C))}`);
    }
    const relist = await waitRequest(requests, from, (r) => r.path === "/api/tasks" && r.method === "GET");
    if (!relist || relist.params.getAll("tag").length) {
      failures.push(`a deleted tag must be dropped from the active filter; next list request ${relist ? relist.params.toString() : "none"}`);
    }
    const filterChips = await page.evaluate(() =>
      [...document.querySelectorAll("#filter-tags .tag-chip-label")].map((e) => e.textContent));
    if (filterChips.length) failures.push(`the filter should no longer show the deleted tag; chips ${JSON.stringify(filterChips)}`);

    // ---- cancelling the confirmation deletes nothing ----
    from = requests.length;
    page.once("dialog", (d) => void d.dismiss());
    await page.click(`${rowSel("t-old")} .tag-row-delete`, { timeout: 2000 }).catch(() => {});
    await page.waitForTimeout(200);
    if (requests.slice(from).some((r) => r.method === "DELETE")) failures.push("dismissing the confirmation still deleted the tag");

    // ---- create ----
    from = requests.length;
    await page.fill("#tag-create-input", "Новый").catch(() => failures.push("no #tag-create-input"));
    await page.keyboard.press("Enter");
    const post = await waitRequest(requests, from, (r) => r.method === "POST" && r.path === "/api/tags");
    if (!post || post.body?.name !== "Новый") failures.push(`creating should POST the name; sent ${JSON.stringify(post && post.body)}`);
    const afterCreate = await waitRows(page, (g) => g.some((r) => r.name === "Новый" && r.unused && r.count === ""));
    if (!afterCreate.ok) failures.push(`the new tag should be listed, unused; rows ${JSON.stringify(afterCreate.got)}`);
    const cleared = await page.inputValue("#tag-create-input").catch(() => "?");
    if (cleared !== "") failures.push(`the create field should empty after a create; holds "${cleared}"`);
    from = requests.length;
    await page.fill("#tag-create-input", "job").catch(() => {});
    await page.keyboard.press("Enter");
    await waitRequest(requests, from, (r) => r.method === "POST" && r.path === "/api/tags");
    const wantExists = await page.evaluate(() => t("tags.exists"));
    const createErr = await page.waitForFunction((w) => {
      const el = document.getElementById("tag-create-error");
      return !!el && el.offsetParent !== null && el.textContent.trim() === w;
    }, wantExists, { timeout: 3000 }).then(() => true).catch(() => false);
    if (!createErr) failures.push(`creating an existing name should show "${wantExists}"`);

    // ---- 375px: the dialog fits ----
    const phone = await browser.newPage({ viewport: { width: 375, height: 800 } });
    phone.on("pageerror", (e) => errors.push("pageerror(375): " + e.message));
    await phone.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await phone.waitForSelector(T2C, { timeout: 8000 }).catch(() => {});
    if (await openDialog(phone)) {
      await waitRows(phone, (g) => g.length >= 3);
      const measure = () => phone.evaluate((d) => {
        const el = document.querySelector(d);
        const r = el.getBoundingClientRect();
        const list = el.querySelector(".tags-list");
        const edge = list.getBoundingClientRect().right;
        const rowsOut = [...el.querySelectorAll(".tag-row *")]
          .filter((c) => c.getBoundingClientRect().right > edge + 1).length;
        return {
          inner: el.scrollWidth - el.clientWidth,
          list: list.scrollWidth - list.clientWidth,
          rowsOut,
          page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          right: r.right, vw: window.innerWidth,
        };
      }, D);
      // The 64-character name as a row, then in its rename input.
      for (const mode of ["row", "rename"]) {
        if (mode === "rename") {
          await phone.click(`${rowSel("t-long")} .tag-row-rename`, { timeout: 2000 }).catch(() => {});
          await phone.waitForSelector(`${rowSel("t-long")} .tag-row-input`, { timeout: 2000 }).catch(() => {});
        }
        const fit = await measure();
        if (fit.inner > 1 || fit.list > 1 || fit.rowsOut) {
          failures.push(`at 375px (${mode}) the dialog or its list scrolls sideways: ${JSON.stringify(fit)}`);
        }
        if (fit.page > 1 || fit.right > fit.vw + 1) {
          failures.push(`at 375px (${mode}) the dialog sticks out of the viewport: ${JSON.stringify(fit)}`);
        }
      }
    } else {
      failures.push("at 375px the dialog did not open");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

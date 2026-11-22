// Tag filters in the task list and the library (vts-qwcj).
//
// Each filter bar gets a tag picker (the tag field with allowCreate:false, so
// only existing tags can be chosen) and an all/any toggle that shows once two
// or more tags are chosen. The chosen tags go to the server as repeated `tag`
// params with `tag_match` — on the first page, the next page, the count and,
// in the library, the per-task banner lookup — exactly like q and the dates.
// The stub filters by those params the way the endpoints do, so the cards and
// the count on screen are the server's answer to what was sent.
//
// The task filter survives a reload like the other task filters; clearing
// drops the tags. At 375px neither bar scrolls the page sideways with a
// 64-character tag chosen.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR } from "../harness.mjs";

export const name = "tags-filters";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const LONG = "Очень длинное имя тега, которое должно переноситься внутри чипа 12".slice(0, 64);
const TAGS = [
  { id: "t-fam", name: "Family", count: 3 },
  { id: "t-old", name: "Old", count: 0 },
  { id: "t-work", name: "Work", count: 3 },
  { id: "t-long", name: LONG, count: 1 },
];
const W = { id: "t-work", name: "Work" };
const F = { id: "t-fam", name: "Family" };
const L = { id: "t-long", name: LONG };

const tid = (n) => `eeeeeeee-7777-0000-0000-00000000000${n}`;
const rid = (n) => `ffffffff-7777-0000-0000-00000000000${n}`;
// Newest first: 5, 4, 3, 2, 1.
const ROWS = [
  { n: 5, tags: [F, W] },
  { n: 4, tags: [W] },
  { n: 3, tags: [F] },
  { n: 2, tags: [F, W] },
  { n: 1, tags: [L] },
];

function taskRow({ n, tags }) {
  return {
    id: tid(n), source_url: `https://example.com/${n}`, source_title: `Task ${n}`,
    status: "completed", awaiting_step: null, queue: null, queue_position: null,
    transcript_path: "present", summary_path: null, redacted_path: null, media_path: null,
    options: { transcript: true, prompts: [], prompt_results: [] },
    steps: [], capabilities: {},
    created_at: `2026-11-22T10:00:0${n}.000000`, updated_at: `2026-11-22T10:00:0${n}.000000`,
    progress: {}, stats: {}, tags,
  };
}

function recordingRow({ n, tags }) {
  return {
    id: rid(n), source_task_id: tid(n), title: `Rec ${n}`, title_is_custom: false,
    source_url: `https://example.com/${n}`, duration_sec: 60, language: "ru", tags,
    has_transcript: true, has_redacted: false, has_summary: false, has_media: false,
    prompt_results: [], state: "complete",
    recorded_at: `2026-11-22T10:00:0${n}Z`, listed_at: `2026-11-22T10:00:0${n}Z`,
    created_at: `2026-11-22T10:00:0${n}Z`, updated_at: `2026-11-22T10:00:0${n}Z`,
  };
}

// The endpoints' tag predicate: by name, case-insensitively, all or any.
function tagMatch(row, params) {
  const want = params.getAll("tag").map((s) => s.toLowerCase());
  if (!want.length) return true;
  const have = new Set(row.tags.map((t) => t.name.toLowerCase()));
  return params.get("tag_match") === "any" ? want.some((w) => have.has(w)) : want.every((w) => have.has(w));
}

async function startServer() {
  const requests = [];
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
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
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    requests.push({ method: req.method, path: p, params: url.searchParams });
    res.setHeader("Content-Type", "application/json");
    const json = (value) => res.end(JSON.stringify(value));
    const sp = url.searchParams;
    if (p === "/api/tasks") {
      let rows = ROWS.map(taskRow).filter((t) => tagMatch(t, sp));
      if (sp.get("before_ts")) rows = rows.filter((t) => t.created_at < sp.get("before_ts"));
      if (sp.get("after_ts")) rows = rows.filter((t) => t.created_at > sp.get("after_ts"));
      return json(rows.slice(0, Number(sp.get("limit") || 20)));
    }
    if (p === "/api/tasks/count") {
      return json({ total: ROWS.map(taskRow).filter((t) => tagMatch(t, sp)).length });
    }
    if (p === "/api/recordings") {
      let rows = ROWS.map(recordingRow).filter((r) => tagMatch(r, sp));
      const total = rows.length;
      const taskIds = sp.getAll("task_id");
      if (taskIds.length) rows = rows.filter((r) => taskIds.includes(r.source_task_id));
      if (sp.get("before_ts")) rows = rows.filter((r) => r.listed_at < sp.get("before_ts"));
      return json({ items: rows.slice(0, Number(sp.get("limit") || 30)), total });
    }
    const m = p.match(/^\/api\/tasks\/([^/]+)$/);
    if (m) {
      const row = ROWS.find((r) => tid(r.n) === m[1]);
      return json(row ? taskRow(row) : {});
    }
    const table = {
      "/api/version": { version: "verify" },
      "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
      "/api/push/config": { enabled: false },
      "/api/status-config": { status_flags: {}, tasks_page_size: 2 },
      "/api/tags": TAGS,
      "/api/prompts": [],
      "/api/presets": [],
    };
    return json(p in table ? table[p] : {});
  });
  await new Promise((r) => server.listen(0, r));
  return { server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests, emit };
}

const tagsOf = (params) => params.getAll("tag");

async function waitRequest(requests, from, pred, timeout = 4000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const hit = requests.slice(from).find(pred);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 25));
  }
  return null;
}

const isList = (r) => r.method === "GET" && r.path === "/api/tasks";
const isCount = (r) => r.method === "GET" && r.path === "/api/tasks/count";
const isLib = (r) => r.method === "GET" && r.path === "/api/recordings" && !r.params.get("task_id");
const isLookup = (r) => r.method === "GET" && r.path === "/api/recordings" && !!r.params.get("task_id");
const sent = (r) => (r ? JSON.stringify({ tag: tagsOf(r.params), tag_match: r.params.get("tag_match") }) : "none");
const carries = (r, tags, match) =>
  !!r && JSON.stringify(tagsOf(r.params)) === JSON.stringify(tags) && r.params.get("tag_match") === match;

async function pick(page, host, text) {
  await page.click(`${host} .tag-field-input`);
  await page.fill(`${host} .tag-field-input`, text);
  await page.keyboard.press("Enter");
}

const chipNames = (page, host) => page.evaluate((h) =>
  [...document.querySelectorAll(`${h} .tag-field .tag-chip-label`)].map((e) => e.textContent), host);

// Truly on screen: `.segmented` is display:flex, which beats a bare .hidden.
const shown = (page, sel) => page.evaluate((s) => {
  const el = document.querySelector(s);
  return !!el && el.offsetParent !== null && el.getBoundingClientRect().height > 0;
}, sel);

const cardIds = (page, list) => page.evaluate((l) =>
  [...document.querySelectorAll(`${l} > .task`)].map((e) => e.dataset.taskId || e.dataset.recordingId), list);

async function waitCards(page, list, want) {
  return page.waitForFunction(([l, w]) => {
    const got = [...document.querySelectorAll(`${l} > .task`)].map((e) => e.dataset.taskId || e.dataset.recordingId);
    return JSON.stringify(got) === JSON.stringify(w);
  }, [list, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

async function waitText(page, sel, want) {
  return page.waitForFunction(([s, w]) => {
    const el = document.querySelector(s);
    return !!el && !el.classList.contains("hidden") && el.textContent.trim() === w;
  }, [sel, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

export async function run() {
  const failures = [];
  const { server, baseUrl, requests, emit } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const ready = await page.waitForSelector(`#task-list [data-task-id="${tid(5)}"]`, { timeout: 8000 })
      .then(() => true).catch(() => false);
    if (!ready) {
      failures.push("the task list never rendered — nothing below proves anything");
      return failures;
    }
    const picker = await page.waitForSelector("#filter-tags .tag-field-input", { timeout: 2000 })
      .then(() => true).catch(() => false);
    if (!picker) {
      failures.push("the task filter bar has no tag picker (#filter-tags .tag-field-input)");
    } else {
      // ---- one tag: no toggle, no tag_match ----
      let from = requests.length;
      await pick(page, "#filter-tags", "wor");
      let hit = await waitRequest(requests, from, (r) => isList(r) && tagsOf(r.params).length > 0);
      if (!hit || JSON.stringify(tagsOf(hit.params)) !== JSON.stringify(["Work"]) || hit.params.get("tag_match")) {
        failures.push(`picking "Work" should send tag=Work and no tag_match; sent ${sent(hit)}`);
      }
      if (await shown(page, "#filter-tags-match")) failures.push("the all/any toggle must stay hidden with one tag");

      // ---- a name that is not a tag is not taken ----
      await pick(page, "#filter-tags", "Nope");
      const afterNope = await chipNames(page, "#filter-tags");
      if (JSON.stringify(afterNope) !== JSON.stringify(["Work"])) {
        failures.push(`the filter picker must only take existing tags; chips: ${JSON.stringify(afterNope)}`);
      }
      await page.fill("#filter-tags .tag-field-input", "");

      // ---- two tags: all ----
      from = requests.length;
      await pick(page, "#filter-tags", "fam");
      hit = await waitRequest(requests, from, (r) => isList(r) && tagsOf(r.params).length === 2);
      if (!carries(hit, ["Work", "Family"], "all")) {
        failures.push(`two tags should send tag=Work&tag=Family&tag_match=all; sent ${sent(hit)}`);
      }
      if (!(await shown(page, "#filter-tags-match"))) failures.push("the all/any toggle should show with two tags");
      const allActive = await page.evaluate(() =>
        document.querySelector('#filter-tags-match button[data-match="all"]')?.classList.contains("active"));
      if (!allActive) failures.push("\"all\" should be the active choice by default");
      if (!(await waitCards(page, "#task-list", [tid(5), tid(2)]))) {
        failures.push(`with all of [Work, Family] the list should be tasks 5 and 2; got ${JSON.stringify(await cardIds(page, "#task-list"))}`);
      }
      const countAll = await waitRequest(requests, from, (r) => isCount(r) && carries(r, ["Work", "Family"], "all"));
      if (!countAll) failures.push("the task count must be asked with the same tag params");
      if (!(await waitText(page, "#tasks-count", "2"))) {
        failures.push(`the count should read 2 for all of [Work, Family]; got "${await page.textContent("#tasks-count").catch(() => "?")}"`);
      }

      // ---- any; paging carries the tags ----
      from = requests.length;
      await page.click('#filter-tags-match button[data-match="any"]', { timeout: 2000 }).catch(() => {});
      hit = await waitRequest(requests, from, (r) => isList(r) && r.params.get("tag_match") === "any");
      if (!carries(hit, ["Work", "Family"], "any")) {
        failures.push(`switching to "any" should send tag_match=any with both tags; sent ${sent(hit)}`);
      }
      if (!(await waitText(page, "#tasks-count", "4"))) {
        failures.push(`the count should read 4 for any of [Work, Family]; got "${await page.textContent("#tasks-count").catch(() => "?")}"`);
      }
      await page.click("#task-load-more", { timeout: 2000 }).catch(() => {});
      const page2 = await waitRequest(requests, from, (r) => isList(r) && r.params.get("before_ts"));
      if (!carries(page2, ["Work", "Family"], "any")) {
        failures.push(`the next page must carry the tag filter; sent ${sent(page2)}`);
      }
      if (!(await waitCards(page, "#task-list", [tid(5), tid(4), tid(3), tid(2)]))) {
        failures.push(`with any of [Work, Family] the list should be tasks 5, 4, 3, 2; got ${JSON.stringify(await cardIds(page, "#task-list"))}`);
      }

      // ---- survives a reload ----
      from = requests.length;
      await page.reload({ waitUntil: "domcontentloaded" });
      const first = await waitRequest(requests, from, isList, 8000);
      if (!carries(first, ["Work", "Family"], "any")) {
        failures.push(`the first list request after a reload must carry the saved tags; sent ${sent(first)}`);
      }
      await page.waitForSelector("#filter-tags .tag-chip", { timeout: 4000 }).catch(() => {});
      const restored = await chipNames(page, "#filter-tags");
      if (JSON.stringify(restored) !== JSON.stringify(["Work", "Family"])) {
        failures.push(`the tag filter should be restored after a reload; chips: ${JSON.stringify(restored)}`);
      }
      const anyActive = await page.evaluate(() =>
        document.querySelector('#filter-tags-match button[data-match="any"]')?.classList.contains("active"));
      if (!anyActive) failures.push("\"any\" should be restored after a reload");
      const newChip = await page.$("#filter-tags .tag-chip--new");
      if (newChip) failures.push("restored filter chips are existing tags and must not read as new");

      // ---- clear drops the tags ----
      from = requests.length;
      await page.click("#filter-clear", { timeout: 2000 }).catch(() => {});
      hit = await waitRequest(requests, from, isList);
      if (!hit || tagsOf(hit.params).length || hit.params.get("tag_match")) {
        failures.push(`clearing the filters should drop the tag params; sent ${sent(hit)}`);
      }
      if ((await chipNames(page, "#filter-tags")).length) failures.push("clearing the filters should remove the tag chips");
      if (await shown(page, "#filter-tags-match")) failures.push("the toggle should hide after clearing");
    }

    // ---- library ----
    await page.evaluate(() => { libraryPaging.pageSize = 2; });
    await page.click("#main-tab-library");
    await page.waitForSelector(`#library-list [data-recording-id="${rid(5)}"]`, { timeout: 8000 }).catch(() => {});
    const libPicker = await page.$("#library-tags .tag-field-input");
    if (!libPicker) {
      failures.push("the library filter bar has no tag picker (#library-tags .tag-field-input)");
    } else {
      let from = requests.length;
      await pick(page, "#library-tags", "Work");
      await pick(page, "#library-tags", "Family");
      let hit = await waitRequest(requests, from, (r) => isLib(r) && tagsOf(r.params).length === 2);
      if (!carries(hit, ["Work", "Family"], "all")) {
        failures.push(`the library should send tag=Work&tag=Family&tag_match=all; sent ${sent(hit)}`);
      }
      if (!(await waitCards(page, "#library-list", [rid(5), rid(2)]))) {
        failures.push(`library with all of [Work, Family]: ${JSON.stringify(await cardIds(page, "#library-list"))}`);
      }
      from = requests.length;
      await page.click('#library-tags-match button[data-match="any"]', { timeout: 2000 }).catch(() => {});
      hit = await waitRequest(requests, from, (r) => isLib(r) && r.params.get("tag_match") === "any");
      if (!carries(hit, ["Work", "Family"], "any")) {
        failures.push(`library "any" should send tag_match=any; sent ${sent(hit)}`);
      }
      await page.waitForFunction(() => libraryPaging.firstPageLoaded && !libraryPaging.loading, null, { timeout: 4000 }).catch(() => {});
      await page.click("#library-load-more", { timeout: 2000 }).catch(() => {});
      const libPage2 = await waitRequest(requests, from, (r) => isLib(r) && r.params.get("before_ts"));
      if (!carries(libPage2, ["Work", "Family"], "any")) {
        failures.push(`the library's next page must carry the tag filter; sent ${sent(libPage2)}`);
      }
      if (!(await waitText(page, "#library-count", "4"))) {
        failures.push(`the library count should follow the tag filter (4); got "${await page.textContent("#library-count").catch(() => "?")}"`);
      }
      // The per-task lookup behind the banner and live repaints.
      from = requests.length;
      emit("task_status", { task_id: tid(3), data: { status: "completed" } });
      const lookup = await waitRequest(requests, from, isLookup);
      if (!carries(lookup, ["Work", "Family"], "any")) {
        failures.push(`the library's per-task lookup must carry the tag filter; sent ${sent(lookup)}`);
      }
      from = requests.length;
      await page.click("#library-clear", { timeout: 2000 }).catch(() => {});
      hit = await waitRequest(requests, from, isLib);
      if (!hit || tagsOf(hit.params).length) failures.push(`clearing the library filters should drop the tags; sent ${sent(hit)}`);
      if ((await chipNames(page, "#library-tags")).length) failures.push("clearing the library filters should remove the chips");
    }

    // ---- 375px: a 64-character chip in either bar ----
    const phone = await browser.newPage({ viewport: { width: 375, height: 800 } });
    phone.on("pageerror", (e) => errors.push("pageerror(375): " + e.message));
    await phone.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await phone.waitForSelector("#filter-tags .tag-field-input", { timeout: 8000 }).catch(() => {});
    if (await phone.$("#filter-tags .tag-field-input")) {
      await pick(phone, "#filter-tags", "Очень");
      await pick(phone, "#filter-tags", "Work");
      await phone.waitForSelector("#filter-tags-match button", { state: "visible", timeout: 3000 }).catch(() => {});
      const over = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (over > 1) failures.push(`at 375px the task filter bar with a long tag scrolls the page by ${over}px`);
      await phone.click("#main-tab-library");
      await phone.waitForSelector("#library-tags .tag-field-input", { state: "visible", timeout: 4000 }).catch(() => {});
      if (await phone.$("#library-tags .tag-field-input")) {
        await pick(phone, "#library-tags", "Очень");
        await pick(phone, "#library-tags", "Family");
        await phone.waitForTimeout(150);
        const over2 = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        if (over2 > 1) failures.push(`at 375px the library filter bar with a long tag scrolls the page by ${over2}px`);
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

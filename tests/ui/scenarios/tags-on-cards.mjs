// Tags on cards (vts-qwcj): the read-only row and "Edit tags".
//
// A task card and a library card show the same recording's tags in a
// `.task-tags` row under the title (hidden when there are none). "Edit tags"
// in the card menu swaps the row for the tag field; Enter on an empty input or
// leaving the field saves, Esc cancels. The save goes to the route that owns
// the card — PUT /api/tasks/{id}/tags from a task card, PUT
// /api/recordings/{rid}/tags from a library card — with the FULL list, and the
// card then shows the SERVER's answer (here sorted differently from what was
// typed, so a card painting its own guess fails). The same recording's card in
// the other list, when loaded, is patched too.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR } from "../harness.mjs";

export const name = "tags-on-cards";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const TASK = "dddddddd-6666-0000-0000-000000000001";
const EMPTY = "dddddddd-6666-0000-0000-000000000002";
const LONGTAGGED = "dddddddd-6666-0000-0000-000000000003";
const REC = "aaaaaaaa-6666-0000-0000-000000000001";
const LONG = "Очень длинное имя тега, которое должно переноситься внутри чипа 12".slice(0, 64);
const KNOWN = [
  { id: "t-biz", name: "Бизнес-клуб", count: 1 },
  { id: "t-fam", name: "Семья", count: 0 },
];

function taskRow(id, title, tags) {
  return {
    id, source_url: "https://example.com/v", source_title: title,
    status: "completed", awaiting_step: null, queue: null, queue_position: null,
    transcript_path: "present", summary_path: null, redacted_path: null, media_path: null,
    options: { transcript: true, prompts: [], prompt_results: [] },
    steps: [], capabilities: {},
    created_at: `2026-11-22T10:00:0${id.endsWith("1") ? 1 : 0}.000000`,
    updated_at: "2026-11-22T10:05:00.000000",
    progress: {}, stats: {}, tags,
  };
}

function recording(tags) {
  return {
    id: REC, source_task_id: TASK, title: "Tagged", title_is_custom: false,
    source_url: "https://example.com/v", duration_sec: 60, language: "ru", tags,
    has_transcript: true, has_redacted: false, has_summary: false, has_media: false,
    prompt_results: [], state: "complete",
    recorded_at: "2026-11-22T10:00:00Z", listed_at: "2026-11-22T10:00:00Z",
    created_at: "2026-11-22T10:00:00Z", updated_at: "2026-11-22T10:05:00Z",
  };
}

// What the real endpoint answers: names resolved to tags (an existing key
// keeps the stored spelling), sorted by name.
function setTags(names) {
  let n = 0;
  return names
    .map((nm) => KNOWN.find((t) => t.name.toLowerCase() === nm.toLowerCase()) || { id: `t-made-${n++}`, name: nm })
    .map(({ id, name }) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

async function startServer() {
  const requests = [];
  const initial = [{ id: "t-biz", name: "Бизнес-клуб" }];
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
      const text = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = JSON.parse(text || "null"); } catch { body = null; }
      requests.push({ method: req.method, path: p, body });
      const json = (value) => { res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify(value)); };
      if (req.method === "PUT" && /^\/api\/(tasks|recordings)\/[^/]+\/tags$/.test(p)) {
        return json(setTags((body && body.names) || []));
      }
      if (req.method !== "GET") return json({ status: "ok" });
      const table = {
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/tasks": [
          taskRow(TASK, "Tagged", initial), taskRow(EMPTY, "Untagged", []),
          taskRow(LONGTAGGED, "Long tag", [{ id: "t-long", name: LONG }]),
        ],
        "/api/recordings": { items: [recording(initial)], total: 1 },
        "/api/tags": KNOWN,
        "/api/prompts": [],
        "/api/presets": [],
        [`/api/tasks/${TASK}/speaker-matches`]: [],
      };
      return json(p in table ? table[p] : {});
    });
  });
  await new Promise((r) => server.listen(0, r));
  return { server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests };
}

const TASK_CARD = `#task-list [data-task-id="${TASK}"]`;
const EMPTY_CARD = `#task-list [data-task-id="${EMPTY}"]`;
const LIB_CARD = `#library-list [data-recording-id="${REC}"]`;

const rowNames = (page, card) => page.evaluate((c) => {
  const row = document.querySelector(`${c} .task-tags`);
  if (!row) return null;
  return [...row.querySelectorAll(".tag-chip-label")].map((e) => e.textContent);
}, card);

async function waitRow(page, card, want) {
  return page.waitForFunction(([c, w]) => {
    const row = document.querySelector(`${c} .task-tags`);
    if (!row || row.hidden || row.classList.contains("hidden")) return w.length === 0 && !!row;
    const got = [...row.querySelectorAll(".tag-chip-label")].map((e) => e.textContent);
    return JSON.stringify(got) === JSON.stringify(w);
  }, [card, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

async function openEditor(page, card) {
  await page.click(`${card} .task-menu-btn`);
  const item = await page.waitForSelector(`${card} .task-menu.open .task-edit-tags-btn`, { state: "visible", timeout: 3000 })
    .then(() => true).catch(() => false);
  if (!item) return false;
  await page.click(`${card} .task-menu.open .task-edit-tags-btn`);
  return page.waitForSelector(`${card} .task-tags-edit .tag-field-input`, { state: "visible", timeout: 3000 })
    .then(() => true).catch(() => false);
}

const editorOpen = (page, card) => page.evaluate((c) => {
  const el = document.querySelector(`${c} .task-tags-edit`);
  return !!el && !el.classList.contains("hidden") && el.offsetParent !== null;
}, card);

async function waitRequest(requests, from, pred, timeout = 4000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const hit = requests.slice(from).find(pred);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 25));
  }
  return null;
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
      if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const loaded = await page.waitForSelector(EMPTY_CARD, { timeout: 8000 }).then(() => true).catch(() => false);
    if (!loaded) {
      failures.push("the task list never rendered — nothing below proves anything");
      return failures;
    }

    // ---- read-only rows ----
    if (!(await waitRow(page, TASK_CARD, ["Бизнес-клуб"]))) {
      failures.push(`the task card should show its tags under the title; row: ${JSON.stringify(await rowNames(page, TASK_CARD))}`);
    }
    const emptyShown = await page.evaluate((c) => {
      const row = document.querySelector(`${c} .task-tags`);
      return !!row && row.offsetParent !== null && row.getBoundingClientRect().height > 0;
    }, EMPTY_CARD);
    if (emptyShown) failures.push("a card with no tags must hide its tags row");

    await page.click("#main-tab-library");
    await page.waitForSelector(LIB_CARD, { timeout: 8000 }).catch(() => {});
    if (!(await waitRow(page, LIB_CARD, ["Бизнес-клуб"]))) {
      failures.push(`the library card should show the recording's tags; row: ${JSON.stringify(await rowNames(page, LIB_CARD))}`);
    }
    await page.click("#main-tab-tasks");
    await page.waitForSelector(TASK_CARD, { state: "visible", timeout: 3000 }).catch(() => {});

    // ---- task card: Enter on an empty input saves via the task route ----
    if (!(await openEditor(page, TASK_CARD))) {
      failures.push("\"Edit tags\" on a task card did not open the tag field");
    } else {
      const inp = `${TASK_CARD} .task-tags-edit .tag-field-input`;
      const shown = await page.$$eval(`${TASK_CARD} .task-tags-edit .tag-chip-label`, (els) => els.map((e) => e.textContent));
      if (JSON.stringify(shown) !== JSON.stringify(["Бизнес-клуб"])) {
        failures.push(`the editor should start from the card's tags; chips: ${JSON.stringify(shown)}`);
      }
      const from = requests.length;
      await page.type(inp, "Альфа");
      await page.keyboard.press("Enter"); // adds the chip
      await page.keyboard.press("Enter"); // empty input: saves
      const put = await waitRequest(requests, from, (r) => r.method === "PUT" && r.path.endsWith("/tags"));
      if (!put) failures.push("saving tags on a task card sent no PUT");
      else {
        if (put.path !== `/api/tasks/${TASK}/tags`) failures.push(`a task card saved to ${put.path}, expected /api/tasks/${TASK}/tags`);
        if (JSON.stringify(put.body && put.body.names) !== JSON.stringify(["Бизнес-клуб", "Альфа"])) {
          failures.push(`the PUT should carry the full list; names = ${JSON.stringify(put.body && put.body.names)}`);
        }
      }
      if (!(await waitRow(page, TASK_CARD, ["Альфа", "Бизнес-клуб"]))) {
        failures.push(`after save the task card should show the server's list; row: ${JSON.stringify(await rowNames(page, TASK_CARD))}`);
      }
      if (await editorOpen(page, TASK_CARD)) failures.push("the editor should close after a save");
      if (!(await waitRow(page, LIB_CARD, ["Альфа", "Бизнес-клуб"]))) {
        failures.push(`the same recording's library card should be patched; row: ${JSON.stringify(await rowNames(page, LIB_CARD))}`);
      }
    }

    // ---- library card: leaving the field saves via the recording route ----
    await page.click("#main-tab-library");
    await page.waitForSelector(LIB_CARD, { state: "visible", timeout: 3000 }).catch(() => {});
    if (!(await openEditor(page, LIB_CARD))) {
      failures.push("\"Edit tags\" on a library card did not open the tag field");
    } else {
      const from = requests.length;
      await page.evaluate((c) => {
        const chip = [...document.querySelectorAll(`${c} .task-tags-edit .tag-chip`)]
          .find((ch) => ch.querySelector(".tag-chip-label")?.textContent === "Альфа");
        chip?.querySelector(".tag-chip-remove")?.setAttribute("data-test-target", "1");
      }, LIB_CARD);
      await page.click(`${LIB_CARD} [data-test-target="1"]`, { timeout: 3000 }).catch(() => {});
      // Removing a chip must not count as leaving the field.
      if (requests.slice(from).some((r) => r.method === "PUT")) failures.push("clicking × saved immediately instead of keeping the editor open");
      // Somewhere harmless outside the card: the already-active Library tab.
      await page.click("#main-tab-library");
      const put = await waitRequest(requests, from, (r) => r.method === "PUT" && r.path.endsWith("/tags"));
      if (!put) failures.push("leaving the tag field on a library card sent no PUT");
      else {
        if (put.path !== `/api/recordings/${REC}/tags`) failures.push(`a library card saved to ${put.path}, expected /api/recordings/${REC}/tags`);
        if (JSON.stringify(put.body && put.body.names) !== JSON.stringify(["Бизнес-клуб"])) {
          failures.push(`library PUT names = ${JSON.stringify(put.body && put.body.names)}, expected ["Бизнес-клуб"]`);
        }
      }
      if (!(await waitRow(page, LIB_CARD, ["Бизнес-клуб"]))) {
        failures.push(`after save the library card should show the server's list; row: ${JSON.stringify(await rowNames(page, LIB_CARD))}`);
      }
      if (!(await waitRow(page, TASK_CARD, ["Бизнес-клуб"]))) {
        failures.push(`the task card should be patched from the library save; row: ${JSON.stringify(await rowNames(page, TASK_CARD))}`);
      }
    }

    // ---- a click inside the editor that is not on the input keeps it open;
    //      Esc closes the suggestion list first, then cancels ----
    if (await openEditor(page, LIB_CARD)) {
      const from = requests.length;
      await page.click(`${LIB_CARD} .task-tags-edit .tag-chip-label`, { timeout: 3000 }).catch(() => {});
      await page.click(`${LIB_CARD} .task-tags-edit .tag-field`, { position: { x: 3, y: 3 }, timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(300); // a stray leave-save would land within this
      if (requests.slice(from).some((r) => r.method === "PUT")) failures.push("clicking a chip label / empty space in the editor saved it");
      if (!(await editorOpen(page, LIB_CARD))) failures.push("clicking a chip label / empty space in the editor closed it");
      await page.evaluate((c) => {
        document.querySelector(`${c} .task-tags-edit .tag-chip .tag-chip-remove`)?.setAttribute("data-test-target", "2");
      }, LIB_CARD);
      await page.click(`${LIB_CARD} [data-test-target="2"]`, { timeout: 3000 }).catch(() => {});
      const listOpen = () => page.evaluate((c) => {
        const l = document.querySelector(`${c} .task-tags-edit .tag-suggestions`);
        return !!l && !l.classList.contains("hidden");
      }, LIB_CARD);
      if (!(await listOpen())) failures.push("the suggestion list should be open while editing (setup for the Esc check)");
      await page.keyboard.press("Escape");
      if (await listOpen()) failures.push("the first Esc should close the suggestion list");
      if (!(await editorOpen(page, LIB_CARD))) failures.push("the first Esc (list open) must close the list, not the editor");
      await page.keyboard.press("Escape");
      const closed = await page.waitForFunction((c) => {
        const el = document.querySelector(`${c} .task-tags-edit`);
        return !el || el.classList.contains("hidden") || el.offsetParent === null;
      }, LIB_CARD, { timeout: 3000 }).then(() => true).catch(() => false);
      if (!closed) failures.push("Esc should close the tag editor");
      await page.waitForTimeout(300); // a stray blur-save would land within this
      if (requests.slice(from).some((r) => r.method === "PUT")) failures.push("Esc must cancel, but a PUT was sent");
      if (!(await waitRow(page, LIB_CARD, ["Бизнес-клуб"]))) {
        failures.push(`after Esc the card should show its tags unchanged; row: ${JSON.stringify(await rowNames(page, LIB_CARD))}`);
      }
    } else {
      failures.push("could not reopen the library card's tag editor for the Esc check");
    }

    // ---- mobile: a 64-character tag on a card, read-only and in the editor ----
    const mobile = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await mobile.goto(baseUrl, { waitUntil: "domcontentloaded" });
    const LONG_CARD = `#task-list [data-task-id="${LONGTAGGED}"]`;
    await mobile.waitForSelector(`${LONG_CARD} .task-tags .tag-chip`, { timeout: 8000 }).catch(() => {});
    const sideways = () => mobile.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    let over = await sideways();
    if (over > 0) failures.push(`at 375px a card with a 64-character tag scrolls the page sideways by ${over}px`);
    if (await openEditor(mobile, LONG_CARD)) {
      over = await sideways();
      if (over > 0) failures.push(`at 375px the card's tag editor scrolls the page sideways by ${over}px`);
      await mobile.keyboard.press("Escape");
      await mobile.keyboard.press("Escape");
    } else {
      failures.push("could not open the tag editor at 375px");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

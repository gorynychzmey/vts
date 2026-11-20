// A recording that enters the library while the tab is open is announced by a
// "New recordings (N)" banner, like new tasks are; a click puts it on top
// without rebuilding the cards already there (vts-lzad).
//
// The signals are the existing task events: a task_status or a transcript
// segment for a task with no library card makes the page ask the server for
// that task's recording. Only one that is listed AND newer than the top card
// is announced.
//
// The SSE stream is held open and events are pushed into it: a stub answering
// /api/events with JSON makes the page reconnect and reload lists on its own,
// which would hide a missing live update.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { STATIC_DIR, launch } from "../harness.mjs";

export const name = "library-new-recordings-banner";

const OLD_A = "aaaaaaaa-0000-0000-0000-000000000002";
const OLD_B = "aaaaaaaa-0000-0000-0000-000000000001";
const NEW_REC = "aaaaaaaa-0000-0000-0000-000000000003";
const NEW_TASK = "bbbbbbbb-0000-0000-0000-000000000003";
// A task whose recording is not in the library (no text yet, or filtered out).
const UNLISTED_TASK = "bbbbbbbb-0000-0000-0000-000000000009";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

function recording(id, taskId, listedAt, title) {
  return {
    id, source_task_id: taskId, title, title_is_custom: false,
    source_url: `file://${title}.m4a`, duration_sec: 630, language: "ru", tags: [],
    has_transcript: true, has_redacted: false, has_summary: false,
    has_media: false, prompt_results: [], state: "complete",
    recorded_at: listedAt, listed_at: listedAt,
    created_at: "2026-08-20T09:00:00Z", updated_at: listedAt,
  };
}

// Newest first by (listed_at, id), as the server orders the library.
function cmp(a, b) {
  if (a.listed_at !== b.listed_at) return a.listed_at < b.listed_at ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

async function startServer(library) {
  const requests = [];
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
  };
  const server = http.createServer((req, res) => {
    const [url, qs] = req.url.split("?");
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (url === "/api/recordings") {
        const p = new URLSearchParams(qs || "");
        requests.push(p);
        const listed = library.slice().sort(cmp);
        let items;
        const taskIds = p.getAll("task_id");
        if (taskIds.length) {
          items = listed.filter((r) => taskIds.includes(r.source_task_id));
        } else if (p.get("after_ts")) {
          const after = { listed_at: p.get("after_ts"), id: p.get("after_id") };
          items = listed.filter((r) => cmp(r, after) > 0);
          if (p.get("order") !== "asc") items.reverse();
        } else {
          items = listed.reverse();
        }
        const limit = Number(p.get("limit") || 50);
        res.end(JSON.stringify({ items: items.slice(0, limit), total: library.length }));
        return;
      }
      res.end(JSON.stringify(url === "/api/tasks" ? [] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") body = body.replaceAll("__VTS_VERSION__", "verify");
    res.setHeader("Content-Type", CT[path.extname(fp)] || "text/plain");
    res.end(body);
  });
  await new Promise((r) => server.listen(0, r));
  return {
    server, requests, emit,
    baseUrl: `http://localhost:${server.address().port}`,
    connected: () => Boolean(sseRes),
  };
}

const lookupsFor = (requests, taskId) =>
  requests.filter((p) => p.getAll("task_id").includes(taskId)).length;
// A request for the list itself: no cursor and no task lookup. One is the
// initial load; a second one would mean the list was reloaded from scratch.
const fullLoads = (requests) =>
  requests.filter((p) => !p.get("task_id") && !p.get("after_ts") && !p.get("before_ts")).length;

// Polls a condition on the stub side (requests seen, stream open).
async function waitUntil(cond, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (cond()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return cond();
}

export async function run() {
  const failures = [];
  const library = [
    recording(OLD_A, "bbbbbbbb-0000-0000-0000-000000000002", "2026-08-20T11:00:00Z", "older-a"),
    recording(OLD_B, "bbbbbbbb-0000-0000-0000-000000000001", "2026-08-20T10:00:00Z", "older-b"),
  ];
  const { server, baseUrl, requests, emit, connected } = await startServer(library);
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) errors.push("console.error: " + m.text());
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length === 2,
      null, { timeout: 8000 },
    );
    if (!(await waitUntil(connected, 5000))) {
      failures.push("setup: the page never opened the SSE stream");
      return failures;
    }
    // Mark the existing cards: after the banner click they must be the SAME
    // nodes, not rebuilt copies (a rebuild collapses whatever the user opened).
    await page.evaluate(() => {
      document.querySelectorAll("#library-list .task").forEach((el) => { el.dataset.marker = "before"; });
    });

    // 1. A task whose recording is not listed: looked up, not announced.
    emit("transcript_segment_text", { task_id: UNLISTED_TASK, data: { text: "hello" } });
    const unlistedLooked = await waitUntil(() => lookupsFor(requests, UNLISTED_TASK) > 0, 4000);
    if (!unlistedLooked) {
      failures.push("a transcript segment for a task without a card did not look up its recording");
    } else {
      await page.waitForTimeout(400); // let the empty answer be processed
      const shown = await page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden === false);
      if (shown) failures.push("the banner appeared for a task whose recording is not listed");
    }

    // 2. A newer recording enters the library. A burst of events for the same
    // task must cost one lookup, not one per event.
    library.push(recording(NEW_REC, NEW_TASK, "2026-08-20T12:00:00Z", "newest"));
    for (let i = 0; i < 4; i += 1) {
      emit("task_status", { task_id: NEW_TASK, data: { status: "transcribing" } });
    }
    const bannerUp = await page.waitForFunction(
      () => {
        const b = document.getElementById("new-recordings-banner");
        return b && !b.hidden && b.offsetParent !== null;
      },
      null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!bannerUp) {
      failures.push("no visible #new-recordings-banner after a newer recording was announced");
      return failures;
    }
    const count = await page.evaluate(() => document.getElementById("new-recordings-count")?.textContent || "");
    if (count !== "(1)") failures.push(`banner count reads ${JSON.stringify(count)}, expected "(1)"`);
    const label = await page.evaluate(() => document.getElementById("new-recordings-banner")?.textContent || "");
    if (!label.includes("New recordings")) failures.push(`banner label reads ${JSON.stringify(label.trim())}`);
    await page.waitForTimeout(700); // past the debounce: a second lookup would have fired by now
    const lookups = lookupsFor(requests, NEW_TASK);
    if (lookups !== 1) failures.push(`four events for one task made ${lookups} lookups, expected 1`);

    // 3. Click: the new card goes on top, the old ones stay, in order, as the
    // same nodes; the banner goes away.
    await page.click("#new-recordings-banner");
    const grew = await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length === 3,
      null, { timeout: 5000 },
    ).then(() => true, () => false);
    if (!grew) {
      failures.push("clicking the banner did not add the new recording to the list");
      return failures;
    }
    const after = await page.evaluate(() => [...document.querySelectorAll("#library-list .task")].map((el) => ({
      id: el.dataset.recordingId, marker: el.dataset.marker || null,
    })));
    const order = after.map((x) => x.id);
    const want = [NEW_REC, OLD_A, OLD_B];
    if (JSON.stringify(order) !== JSON.stringify(want)) {
      failures.push(`list order ${JSON.stringify(order)}, expected ${JSON.stringify(want)}`);
    }
    for (const x of after.slice(1)) {
      if (x.marker !== "before") failures.push(`card ${x.id} was rebuilt instead of kept`);
    }
    const hidden = await page.evaluate(() => document.getElementById("new-recordings-banner")?.hidden);
    if (!hidden) failures.push("the banner is still shown after its recordings were loaded");
    const loads = fullLoads(requests);
    if (loads !== 1) failures.push(`the library list was fetched from scratch ${loads} times, expected 1`);
    const newer = requests.filter((p) => p.get("after_ts"));
    if (newer.length !== 1) {
      failures.push(`expected one load-newer request, saw ${newer.length}`);
    } else {
      const p = newer[0];
      if (p.get("after_ts") !== "2026-08-20T11:00:00Z" || p.get("after_id") !== OLD_A || p.get("order") !== "asc") {
        failures.push(`load-newer asked for ${p.toString()}, expected after the top card in ascending order`);
      }
    }
    const libCount = await page.evaluate(() => document.getElementById("library-count")?.textContent || "");
    if (libCount !== "3") failures.push(`library count reads ${JSON.stringify(libCount)} after the insert, expected "3"`);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

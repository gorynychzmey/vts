// vts-3iw: creating a task must put its card at the TOP of the list, not
// raise the "new tasks (1)" banner.
//
// Two independent defects produced the reported symptom after infinite scroll
// landed:
//   1. uploadFileChunked() discarded the /finalize response, so `created`
//      stayed null and the chunked path fell through to loadFirstPage().
//   2. The server publishes task_status BEFORE returning the create/finalize
//      response, so for a slow create the SSE event arrives while the request
//      is still in flight — with no card in the DOM yet, maybeFlagNewerTask()
//      flagged the user's OWN task as somebody else's new one.
//
// This drives the real race: the stub holds /finalize open, emits task_status
// for that very task id, and only then answers. A fix that only returns the
// task (defect 1) still fails here, because the event wins the race.
//
// The banner itself must keep working for genuinely foreign tasks — asserted
// at the end, so this cannot pass by simply disabling the banner.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "own-upload-not-flagged-new";

const HEAD_ID = "c1111111-1111-1111-1111-111111111111";
const OWN_ID = "c2222222-2222-2222-2222-222222222222";
const FOREIGN_ID = "c3333333-3333-3333-3333-333333333333";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued:    { is_active:false, is_pending:true,  is_finished:false, shows_progress:false, can_pause:true,  can_resume:false, can_archive:false },
  completed: { is_active:false, is_pending:false, is_finished:true,  shows_progress:true,  can_pause:false, can_resume:false, can_archive:true  },
};

function task(id, status, createdAt, sourceUrl) {
  return {
    id, source_url: sourceUrl || ("http://x/" + id), source_title: null, status,
    queue: null, queue_position: null, transcript_path: null, summary_path: null,
    options: { transcript: true, prompts: [] }, steps: [],
    capabilities: { can_restart_summary: false, can_restart_final_summary: false },
    created_at: createdAt, updated_at: createdAt,
    progress: {}, stats: {},
  };
}

// One existing card, so paging has a head to compare against.
const INITIAL_TASKS = [task(HEAD_ID, "completed", "2026-08-03T10:00:00.000000+00:00")];
// Our upload — newer than head, so without the fix it qualifies as "new".
const OWN_TASK = task(OWN_ID, "queued", "2026-08-03T11:00:00.000000+00:00", "file://clip.mp4");
// Somebody else's task, also newer: the banner MUST still fire for this one.
const FOREIGN_TASK = task(FOREIGN_ID, "queued", "2026-08-03T12:00:00.000000+00:00");

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": INITIAL_TASKS,
    "/api/uploads/config": {
      // 1 byte: forces even a tiny test file down the CHUNKED path.
      chunked_threshold_bytes: 1,
      chunk_bytes: 8388608,
      max_upload_bytes: 2147483648,
    },
    [`/api/tasks/${OWN_ID}`]: OWN_TASK,
    [`/api/tasks/${FOREIGN_ID}`]: FOREIGN_TASK,
  };
  let sseRes = null;
  const emit = (event, payload) => {
    if (!sseRes) return false;
    sseRes.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
    return true;
  };

  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];

    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      sseRes = res;
      req.on("close", () => { if (sseRes === res) sseRes = null; });
      return;
    }

    // The upload session id becomes the task id server-side (uploads_finalize
    // passes task_id=uid), which is what lets the client claim it up front.
    if (url === "/api/uploads/init" && req.method === "POST") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ upload_id: OWN_ID, chunk_size: 8388608 }));
      return;
    }
    if (url === `/api/uploads/${OWN_ID}` && req.method === "PATCH") {
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ received: size, total_size: size }));
      });
      return;
    }
    if (url === `/api/uploads/${OWN_ID}/finalize` && req.method === "POST") {
      // THE RACE: announce the task over SSE, let the browser process it,
      // and only then answer the request that created it.
      emit("task_status", { task_id: OWN_ID, data: { status: "queued" } });
      setTimeout(() => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(OWN_TASK));
      }, 900);
      return;
    }

    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : (url === "/api/tasks" ? [] : {})));
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
    server,
    baseUrl: `http://localhost:${server.address().port}`,
    emit,
    connected: () => Boolean(sseRes),
  };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, emit, connected } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "load" });
    await page.waitForSelector(`[data-task-id="${HEAD_ID}"]`, { timeout: 5000 });

    for (let i = 0; i < 50 && !connected(); i++) await page.waitForTimeout(100);
    if (!connected()) {
      failures.push("browser never opened an EventSource on /api/events");
      return failures;
    }

    const bannerState = () => page.evaluate(() => {
      const b = document.getElementById("new-tasks-banner");
      const c = document.getElementById("new-tasks-count");
      if (!b) return { present: false };
      const cs = getComputedStyle(b);
      return {
        present: true,
        visible: !b.hidden && cs.display !== "none" && cs.visibility !== "hidden" && b.offsetHeight > 0,
        count: c ? c.textContent : null,
      };
    });

    // Real interaction: pick the file source, attach a file, submit the form.
    // The radio is styled/overlaid, so drive it the way the app listens for it
    // rather than fighting the hit-target; the assertion below is still on
    // observable state (which card is on top, is the banner showing).
    await page.evaluate(() => {
      const radio = document.getElementById("source-type-file");
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.waitForTimeout(200);
    await page.setInputFiles("#file-input", {
      name: "clip.mp4", mimeType: "video/mp4", buffer: Buffer.from("0123456789"),
    });
    await page.click("#submit-btn", { force: true });

    // Wait for the upload round-trip (finalize is deliberately held ~900ms).
    await page.waitForTimeout(2500);

    // 1. The card must be in the DOM, and at the TOP of the list.
    const placement = await page.evaluate((ownId) => {
      const cards = Array.from(document.querySelectorAll(".task"));
      return {
        present: cards.some((c) => c.dataset.taskId === ownId),
        firstId: cards.length ? cards[0].dataset.taskId : null,
        count: cards.length,
      };
    }, OWN_ID);

    if (!placement.present) {
      failures.push(
        `the created task's card is not in the list at all (cards=${placement.count}) — ` +
        `the create response was dropped instead of being prepended`
      );
    } else if (placement.firstId !== OWN_ID) {
      failures.push(
        `the created task is not at the top of the list: first card is ${placement.firstId}, expected ${OWN_ID}`
      );
    }

    // 2. The banner must NOT be showing for our own task — the actual report.
    const afterOwn = await bannerState();
    if (!afterOwn.present) {
      failures.push("#new-tasks-banner not found in the DOM");
    } else if (afterOwn.visible) {
      failures.push(
        `#new-tasks-banner is visible after the user created their OWN task ` +
        `(count=${JSON.stringify(afterOwn.count)}) — it should have appeared as a card, not as "new tasks"`
      );
    }

    // 3. The banner must still work for a task this tab did NOT create,
    //    so the fix cannot be "turn the banner off".
    emit("task_status", { task_id: FOREIGN_ID, data: { status: "queued" } });
    let foreign = null;
    for (let i = 0; i < 50; i++) {
      foreign = await bannerState();
      if (foreign.visible) break;
      await page.waitForTimeout(100);
    }
    if (!foreign || !foreign.visible) {
      failures.push(
        `#new-tasks-banner did not appear for a FOREIGN task (${FOREIGN_ID}) — ` +
        `the fix suppressed the banner entirely. Last state: ${JSON.stringify(foreign)}`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

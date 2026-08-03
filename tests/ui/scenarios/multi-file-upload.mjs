// vts-vm0: several files upload as ONE task, with aggregate progress.
//
// The single-file flow sends one file and shows its percentage. A set must
// produce exactly one task card, and the ring must show progress across the
// whole set rather than restarting per file.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch } from "../harness.mjs";

export const name = "multi-file-upload";

const TASK_ID = "e1111111-1111-1111-1111-111111111111";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued: { is_active:false, is_pending:true, is_finished:false, shows_progress:false,
            can_pause:true, can_resume:false, can_archive:false },
};

const TASK = {
  id: TASK_ID, source_url: "file://a.mp3", source_title: null, status: "queued",
  queue: null, queue_position: null, transcript_path: null, summary_path: null,
  options: {
    transcript: true, prompts: [],
    source_files: [
      { name: "a.mp3", offset_sec: 0, duration_sec: 10 },
      { name: "b.mp3", offset_sec: 10, duration_sec: 12 },
    ],
    source_files_order: "creation_time",
  },
  steps: [], capabilities: {}, created_at: "2026-08-03T10:00:00.000000+00:00",
  updated_at: "2026-08-03T10:00:00.000000+00:00", progress: {}, stats: {},
};

async function startServer() {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": [],
    "/api/uploads/config": {
      chunked_threshold_bytes: 1, chunk_bytes: 8388608, max_upload_bytes: 2147483648,
    },
  };
  const patched = [];
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(": connected\n\n");
      return;
    }
    if (url === "/api/uploads/init" && req.method === "POST") {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        const parsed = JSON.parse(body || "{}");
        // Mirror the real UploadInitRequest validator (vts/api/schemas.py):
        // either a non-empty `files` array, or both legacy `filename` and
        // `total_size`. A mock that accepts any body would hide the exact
        // class of bug this scenario exists to catch — the client sending a
        // body the real schema 422s on while every browser assertion still
        // passes because nothing here validates the shape.
        const hasFiles = Array.isArray(parsed.files) && parsed.files.length > 0;
        const hasLegacy = typeof parsed.filename === "string" && parsed.filename.length > 0
          && typeof parsed.total_size === "number" && parsed.total_size > 0;
        if (!hasFiles && !hasLegacy) {
          res.statusCode = 422;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ detail: "filename and total_size are required when files is absent" }));
          return;
        }
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({
          upload_id: TASK_ID, chunk_size: 8388608,
          files: (parsed.files || []).map((f, i) => ({ index: i, filename: f.filename })),
        }));
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}` && req.method === "PATCH") {
      const index = new URL("http://x" + req.url).searchParams.get("index");
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        const respond = () => {
          // Recorded only once the response actually goes out, so `patched()`
          // reflects what the client has received, not what the server has
          // merely seen arrive -- the delay below only matters if the two are
          // distinguished this way.
          patched.push({ url: req.url, size });
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ received: size }));
        };
        // Delay index 1's response slightly so there is a real window, after
        // index 0 finishes and before index 1 starts, in which to sample the
        // progress ring. Without this the two chunks (each file fits in one
        // PATCH here) resolve back-to-back and a per-file-only implementation
        // would be indistinguishable from an aggregate one by timing alone.
        if (index === "1") { setTimeout(respond, 300); } else { respond(); }
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}/finalize` && req.method === "POST") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(TASK));
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : {}));
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
  return { server, baseUrl: `http://localhost:${server.address().port}`, patched: () => patched };
}

export async function run() {
  const failures = [];
  const { server, baseUrl, patched } = await startServer();
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

    const acceptsMultiple = await page.evaluate(
      () => document.getElementById("file-input").multiple
    );
    if (!acceptsMultiple) {
      failures.push("#file-input does not accept multiple files");
      return failures;
    }

    await page.evaluate(() => {
      const radio = document.getElementById("source-type-file");
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    // Sizes deliberately differ (1000 vs 3000 bytes, total 4000): after file 0
    // alone finishes, aggregate progress is 1000/4000 = 25%, while a
    // per-file-only implementation would show 100% for file 0. The PATCH for
    // index 1 is delayed 300ms server-side (see startServer) so there is a
    // window to sample the ring in between.
    await page.setInputFiles("#file-input", [
      { name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(1000, 1) },
      { name: "b.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(3000, 2) },
    ]);
    await page.click("#submit-btn", { force: true });

    // Poll until index 0's PATCH has landed but index 1's has not yet -- the
    // 300ms server delay on index 1 gives a real window, but wait
    // deterministically on the actual PATCH log rather than a fixed sleep.
    let ratioAfterFirstFile = null;
    for (let i = 0; i < 50; i += 1) {
      const sentSoFar = patched();
      const hasIndex0 = sentSoFar.some((p) => new URL("http://x" + p.url).searchParams.get("index") === "0");
      const hasIndex1 = sentSoFar.some((p) => new URL("http://x" + p.url).searchParams.get("index") === "1");
      if (hasIndex0 && !hasIndex1) {
        ratioAfterFirstFile = await page.evaluate(() => {
          const fill = document.querySelector("#submit-btn .submit-progress-fill");
          if (!fill) return null;
          const circumference = 56.55;
          const offset = parseFloat(fill.style.strokeDashoffset || "0");
          return 1 - offset / circumference;
        });
        break;
      }
      await page.waitForTimeout(20);
    }
    if (ratioAfterFirstFile === null) {
      failures.push("never observed a moment where index 0 had landed and index 1 had not (timing window missed)");
    } else if (ratioAfterFirstFile > 0.6) {
      failures.push(
        `progress ring showed ${(ratioAfterFirstFile * 100).toFixed(0)}% after only file 0 (1000/4000 bytes) finished -- `
        + `looks like per-file progress restarting rather than aggregate (expected ~25%)`
      );
    }

    await page.waitForTimeout(1000);

    const sent = patched();
    if (sent.length < 2) {
      failures.push(`expected a PATCH per file, saw ${sent.length}: ${JSON.stringify(sent)}`);
    }
    const indices = sent.map((p) => new URL("http://x" + p.url).searchParams.get("index"));
    if (!indices.includes("0") || !indices.includes("1")) {
      failures.push(`PATCHes did not target both indices: ${JSON.stringify(indices)}`);
    }

    const cards = await page.evaluate(() => document.querySelectorAll(".task").length);
    if (cards !== 1) failures.push(`expected exactly 1 task card for the set, got ${cards}`);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

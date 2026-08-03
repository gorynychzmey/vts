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
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({
          upload_id: TASK_ID, chunk_size: 8388608,
          files: (parsed.files || []).map((f, i) => ({ index: i, filename: f.filename })),
        }));
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}` && req.method === "PATCH") {
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        patched.push({ url: req.url, size });
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ received: size }));
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
    await page.setInputFiles("#file-input", [
      { name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(1000, 1) },
      { name: "b.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(3000, 2) },
    ]);
    await page.click("#submit-btn", { force: true });
    await page.waitForTimeout(2500);

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

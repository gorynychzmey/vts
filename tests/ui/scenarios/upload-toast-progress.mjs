// Diana's feedback (vts-ys0s): "неочевидно, сколько файлов у нас загрузилось в
// задачу. Во-первых, сама загрузка не наглядная." Uploading showed only a thin
// ring around the submit button — no filename, no count, no sense of how far
// through a set you were.
//
// Two things are verified here, both black-box against a real upload:
//
//  1. The toast, during a MULTI-file upload: visible, naming the file actually
//     in flight, with a "file N of M" counter and two live bars — the top one
//     across the set, the bottom one within the current file. The two must
//     differ mid-upload; a single shared number would mean one of them is
//     decorative. Sizes are deliberately lopsided (1000 vs 3000 bytes) so
//     "aggregate bytes" and "files done" cannot be confused: after file 0 the
//     set is 25% done, not 50%.
//  2. The toast, during a SINGLE-file upload: the "N of M" files bar is hidden
//     entirely (Victor — two bars showing the same number is noise), while the
//     filename and its own progress bar remain.
//
// Plus the other half of the ask: the finished card's info line reports the
// file count, and only when there is more than one file.
import http from "http";
import fs from "fs";
import path from "path";
import { STATIC_DIR, DEFAULT_API, launch, screenshot } from "../harness.mjs";

export const name = "upload-toast-progress";

const TASK_ID = "e2222222-2222-2222-2222-222222222222";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const FLAGS = {
  queued: { is_active: false, is_pending: true, is_finished: false, shows_progress: false,
            can_pause: true, can_resume: false, can_archive: false },
  completed: { is_active: false, is_pending: false, is_finished: true, shows_progress: false,
               can_pause: false, can_resume: false, can_archive: true },
};

const SOURCE_FILES = [
  { name: "a.mp3", offset_sec: 0, duration_sec: 10 },
  { name: "b.mp3", offset_sec: 10, duration_sec: 12 },
  { name: "c.mp3", offset_sec: 22, duration_sec: 8 },
];

const MULTI_TASK = {
  id: TASK_ID, source_url: "file://a.mp3", source_title: null, status: "queued",
  queue: null, queue_position: null, transcript_path: null, summary_path: null,
  options: { transcript: true, prompts: [], source_files: SOURCE_FILES, source_files_order: "filename" },
  steps: [], capabilities: {},
  created_at: "2026-08-05T10:00:00.000000+00:00",
  updated_at: "2026-08-05T10:00:00.000000+00:00",
  progress: {}, stats: { media_seconds: 30, media_bytes: 4000 },
};

// Same task shape with a single source file: the count must NOT be rendered.
const SINGLE_TASK = {
  ...MULTI_TASK,
  id: "e3333333-3333-3333-3333-333333333333",
  options: { transcript: true, prompts: [], source_files: [SOURCE_FILES[0]] },
};

function startServer({ chunkDelayMs = 0, listTasks = [] } = {}) {
  const api = {
    ...DEFAULT_API,
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/tasks": listTasks,
    // threshold 1 byte -> every upload takes the chunked path, so the toast is
    // driven by the same code the real large uploads use.
    "/api/uploads/config": {
      chunked_threshold_bytes: 1, chunk_bytes: 1000, max_upload_bytes: 2147483648,
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
          upload_id: TASK_ID,
          chunk_size: 1000,
          files: (parsed.files || []).map((f, i) => ({ index: i, filename: f.filename })),
        }));
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}` && req.method === "PATCH") {
      const params = new URL("http://x" + req.url).searchParams;
      const index = params.get("index");
      const offset = Number(params.get("offset") || 0);
      let size = 0;
      req.on("data", (c) => { size += c.length; });
      req.on("end", () => {
        // Slow every chunk a little so the browser has a window in which the
        // toast is mid-flight and can be sampled — otherwise the whole upload
        // resolves between two polls and only the final state is observable.
        setTimeout(() => {
          patched.push({ url: req.url, index, size });
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ received: offset + size }));
        }, chunkDelayMs);
      });
      return;
    }
    if (url === `/api/uploads/${TASK_ID}/finalize` && req.method === "POST") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(MULTI_TASK));
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
  return new Promise((resolve) => {
    server.listen(0, () =>
      resolve({ server, baseUrl: `http://localhost:${server.address().port}`, patched: () => patched }),
    );
  });
}

const readToast = (page) =>
  page.evaluate(() => {
    const el = document.getElementById("upload-toast");
    if (!el) return null;
    const pct = (id) => {
      const fill = document.getElementById(id);
      return fill ? Math.round(parseFloat(fill.style.width || "0")) : null;
    };
    const shown = (id) => {
      const n = document.getElementById(id);
      if (!n) return false;
      const cs = getComputedStyle(n);
      return cs.display !== "none" && cs.visibility !== "hidden";
    };
    return {
      hidden: el.hidden,
      visible: !el.hidden && getComputedStyle(el).display !== "none",
      title: (document.getElementById("upload-toast-title")?.textContent || "").trim(),
      count: (document.getElementById("upload-toast-count")?.textContent || "").trim(),
      file: (document.getElementById("upload-toast-filename")?.textContent || "").trim(),
      filesPct: pct("upload-toast-files-fill"),
      chunksPct: pct("upload-toast-chunks-fill"),
      filesBarShown: shown("upload-toast-files-bar"),
      chunksBarShown: shown("upload-toast-chunks-bar"),
      single: el.classList.contains("single"),
    };
  });

async function pickFileMode(page) {
  await page.evaluate(() => {
    const radio = document.getElementById("source-type-file");
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

export async function run() {
  const failures = [];

  // ---------- multi-file: both bars, counter, filename ----------
  {
    const { server, baseUrl } = await startServer({ chunkDelayMs: 220 });
    const browser = await launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1100, height: 800 }, locale: "ru-RU" });
      const errors = [];
      page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
      await page.goto(baseUrl, { waitUntil: "load" });
      await page.waitForTimeout(300);

      const before = await readToast(page);
      if (!before) {
        failures.push("#upload-toast is not in the DOM at all");
        return failures;
      }
      if (before.visible) {
        failures.push("the upload toast is visible before any upload started");
      }

      await pickFileMode(page);
      // 1000 / 3000 / 1000 bytes, chunk size 1000 -> 5 chunks total, and file 0
      // finishing means 1000/5000 = 20% of the set, not 33%.
      await page.setInputFiles("#file-input", [
        { name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(1000, 1) },
        { name: "b.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(3000, 2) },
        { name: "c.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(1000, 3) },
      ]);
      await page.click("#submit-btn", { force: true });

      // Sample the toast repeatedly while the upload runs.
      const samples = [];
      for (let i = 0; i < 60; i += 1) {
        const snap = await readToast(page);
        if (snap && snap.visible) {
          samples.push(snap);
          // One close-up while it is genuinely on screen and mid-set.
          if (samples.length === 4) {
            const el = await page.$("#upload-toast");
            if (el) await el.screenshot({ path: "/tmp/vts-ui-verify/toast-multi-closeup.png" }).catch(() => {});
          }
        }
        if (snap && !snap.visible && samples.length) break;
        await page.waitForTimeout(60);
      }

      if (!samples.length) {
        failures.push("the upload toast never became visible during a 3-file upload");
      } else {
        const withCount = samples.filter((s) => /\d+\D+\d+/.test(s.count));
        if (!withCount.length) {
          failures.push(
            `the toast never showed a "file N of M" counter (samples: ${JSON.stringify(samples.slice(0, 3).map((s) => s.count))})`,
          );
        }
        if (!samples.some((s) => s.title)) {
          failures.push("the toast never showed a title");
        }
        // The filename must track the file actually in flight.
        const names = [...new Set(samples.map((s) => s.file).filter(Boolean))];
        if (!names.length) {
          failures.push("the toast never showed the name of the file being uploaded");
        } else if (!names.includes("a.mp3")) {
          failures.push(`the toast never named the first file (saw: ${names.join(", ")})`);
        }
        if (names.length < 2) {
          failures.push(
            `the filename never changed as the upload moved between files (saw only: ${names.join(", ")})`,
          );
        }
        // Both bars must be present for a multi-file upload...
        if (samples.some((s) => s.single)) {
          failures.push("a 3-file upload rendered the toast in single-file mode");
        }
        if (!samples.every((s) => s.filesBarShown)) {
          failures.push("the files bar was hidden during a multi-file upload");
        }
        if (!samples.every((s) => s.chunksBarShown)) {
          failures.push("the per-file bar was hidden during a multi-file upload");
        }
        // ...and they must actually differ at some point: two bars showing the
        // same number would mean one is not measuring what it claims.
        const differ = samples.some(
          (s) => Number.isFinite(s.filesPct) && Number.isFinite(s.chunksPct)
            && Math.abs(s.filesPct - s.chunksPct) > 5,
        );
        if (!differ) {
          failures.push(
            "the two bars never differed by more than 5% — the per-file bar looks like a copy of the set bar " +
            `(samples: ${JSON.stringify(samples.slice(0, 6).map((s) => [s.filesPct, s.chunksPct]))})`,
          );
        }
        // The set bar must advance monotonically (the vts-vm0 rule).
        const filesSeries = samples.map((s) => s.filesPct).filter(Number.isFinite);
        for (let i = 1; i < filesSeries.length; i += 1) {
          if (filesSeries[i] < filesSeries[i - 1] - 1) {
            failures.push(
              `the set bar went backwards (${filesSeries[i - 1]}% -> ${filesSeries[i]}%) — ` +
              `it is restarting per file instead of tracking the whole set`,
            );
            break;
          }
        }
        await screenshot(page, "upload-toast-multi");
        // Also shoot the toast alone, from the mid-upload sample above — the
        // page-level shot is mostly form, and by the time it is taken the
        // upload may already have finished and hidden the toast.
        const toastEl = await page.$("#upload-toast");
        if (toastEl && samples.length) {
          await toastEl.screenshot({ path: "/tmp/vts-ui-verify/toast-multi-closeup.png" }).catch(() => {});
        }
      }

      // Once the upload finishes the toast must go away.
      await page.waitForTimeout(1500);
      const after = await readToast(page);
      if (after && after.visible) {
        failures.push("the upload toast stayed visible after the upload finished");
      }

      failures.push(...errors);
      await page.close();
    } finally {
      await browser.close();
      server.close();
    }
  }

  // ---------- single file: no "N of M" bar ----------
  {
    const { server, baseUrl } = await startServer({ chunkDelayMs: 260 });
    const browser = await launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1100, height: 800 }, locale: "ru-RU" });
      await page.goto(baseUrl, { waitUntil: "load" });
      await page.waitForTimeout(300);
      await pickFileMode(page);
      await page.setInputFiles("#file-input", [
        { name: "solo.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(3000, 7) },
      ]);
      await page.click("#submit-btn", { force: true });

      const samples = [];
      for (let i = 0; i < 60; i += 1) {
        const snap = await readToast(page);
        if (snap && snap.visible) {
          samples.push(snap);
          if (samples.length === 3) {
            const el = await page.$("#upload-toast");
            if (el) await el.screenshot({ path: "/tmp/vts-ui-verify/toast-single-closeup.png" }).catch(() => {});
          }
        }
        if (snap && !snap.visible && samples.length) break;
        await page.waitForTimeout(60);
      }

      if (!samples.length) {
        failures.push("the upload toast never became visible during a single-file upload");
      } else {
        if (!samples.every((s) => s.single)) {
          failures.push("a single-file upload did not put the toast in single-file mode");
        }
        if (samples.some((s) => s.filesBarShown)) {
          failures.push(
            "the files bar is still shown for a single-file upload — for one file it carries no information",
          );
        }
        if (!samples.every((s) => s.chunksBarShown)) {
          failures.push("the per-file bar is hidden for a single-file upload — nothing reports progress");
        }
        if (!samples.some((s) => s.file === "solo.mp3")) {
          failures.push(`the toast never named the single file (saw: ${samples.map((s) => s.file).join(",")})`);
        }
        if (samples.some((s) => /\d+\D+\d+/.test(s.count))) {
          failures.push(`a single-file upload showed a "N of M" counter: ${samples.find((s) => s.count).count}`);
        }
        // Progress must actually move, not sit at 0.
        const moved = samples.some((s) => Number.isFinite(s.chunksPct) && s.chunksPct > 0);
        if (!moved) {
          failures.push("the single-file progress bar never advanced past 0%");
        }
        await screenshot(page, "upload-toast-single");
      }
      await page.close();
    } finally {
      await browser.close();
      server.close();
    }
  }

  // ---------- the info line reports the file count ----------
  {
    const { server, baseUrl } = await startServer({ listTasks: [MULTI_TASK, SINGLE_TASK] });
    const browser = await launch();
    try {
      const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, locale: "ru-RU" });
      await page.goto(baseUrl, { waitUntil: "load" });
      await page.waitForTimeout(500);

      const stats = await page.evaluate(() =>
        [...document.querySelectorAll(".task")].map((card) => ({
          id: card.dataset.taskId,
          text: (card.querySelector(".task-stats-text")?.textContent || "").trim(),
        })),
      );
      const multi = stats.find((s) => s.id && s.id.startsWith("e2222222"));
      const single = stats.find((s) => s.id && s.id.startsWith("e3333333"));

      if (!multi) {
        failures.push("the multi-file task card was not rendered");
      } else if (!/3/.test(multi.text)) {
        failures.push(
          `the multi-file card's info line does not report 3 files: ${JSON.stringify(multi.text)}`,
        );
      }
      if (!single) {
        failures.push("the single-file task card was not rendered");
      } else if (/файл/i.test(single.text)) {
        // NB: no \b before "файл" — JS word boundaries are ASCII-only, so
        // /\bфайл/ never matches Cyrillic and the assertion silently passed
        // against a deliberately broken build.
        failures.push(
          `a single-file task should not report a file count, got: ${JSON.stringify(single.text)}`,
        );
      }
      await page.close();
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}

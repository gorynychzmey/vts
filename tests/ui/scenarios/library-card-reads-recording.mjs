// A library card reads its content from the RECORDING (vts-lzad).
//
// The card used to be fed the recording's source_task_id and read every tab
// from /api/tasks/{task}/…, so a library row was only as alive as the job that
// produced it. The recording now has its own content routes, and the library
// card must use them: transcript, processed transcript, summary, a prompt
// result, subtitles and the share dialog all come from /api/recordings/{rid}/….
//
// The stub serves library content ONLY under the recording routes and answers
// 404 on the task routes, so a card still reading through the task shows the
// 404 text instead of the content. Three cards cover the boundary:
//   * an attached recording — content from the recording; task-scoped actions
//     (speakers) still go to its source task;
//   * a detached recording (task deleted, media gone) — content shows, the
//     title still opens the player, no speaker or restart controls, and not a
//     single request goes to /api/tasks/{rid}/…;
//   * a task card in the Tasks view — unchanged, reads /api/tasks/{id}/….
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR } from "../harness.mjs";

export const name = "library-card-reads-recording";

const ATTACHED = "aaaaaaaa-1111-0000-0000-000000000001";
const DETACHED = "aaaaaaaa-1111-0000-0000-000000000002";
const SOURCE_TASK = "cccccccc-1111-0000-0000-000000000001";
const TASK = "dddddddd-1111-0000-0000-000000000001";
const MEMO = "eeeeeeee-1111-0000-0000-000000000001";

const CONTENT = {
  [ATTACHED]: {
    "text/raw": "ATTACHED raw transcript",
    "text/redacted": "ATTACHED processed transcript",
    "text/summary": "ATTACHED summary",
    [`results/user/${MEMO}`]: "ATTACHED memo",
    subtitles: "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nATTACHED cue",
  },
  [DETACHED]: {
    "text/raw": "DETACHED raw transcript",
    "text/redacted": "DETACHED processed transcript",
    "text/summary": "DETACHED summary",
    [`results/user/${MEMO}`]: "DETACHED memo",
    subtitles: "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nDETACHED cue",
  },
};

const TASK_TRANSCRIPT = "TASK transcript via the task route";

function recording(id, sourceTaskId, title) {
  return {
    id, source_task_id: sourceTaskId, title, title_is_custom: false,
    source_url: `file://${title}.m4a`, duration_sec: 630, language: "ru", tags: [],
    has_transcript: true, has_redacted: true, has_summary: true, has_media: true,
    prompt_results: [{ source: "user", id: MEMO, name: "Memo", status: "completed" }],
    state: "complete",
    recorded_at: "2026-11-01T10:00:00Z", listed_at: "2026-11-01T10:00:00Z",
    created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-01T11:00:00Z",
  };
}

const TASK_ROW = {
  id: TASK, source_url: "https://example.com/v", source_title: "A finished job",
  status: "completed", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: "present", summary_path: null, redacted_path: null, media_path: null,
  options: { transcript: true, prompts: [], prompt_results: [] },
  steps: [], capabilities: {},
  created_at: "2026-11-02T10:00:00Z", updated_at: "2026-11-02T10:05:00Z",
  progress: {}, stats: {},
};

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const TASK_CONTENT = /^\/api\/tasks\/[^/]+\/(transcript|redacted|summary|subtitles|transcript-entries|results\/.*)$/;

export async function run() {
  const failures = [];
  const requests = [];

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const p = url.pathname;
    if (p.startsWith("/api/")) {
      requests.push(p);
      // Held open: a JSON answer here makes the page reconnect and reload.
      if (p === "/api/events") {
        res.writeHead(200, {
          "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive",
        });
        res.write(": connected\n\n");
        return;
      }
      const rec = p.match(/^\/api\/recordings\/([^/]+)\/(.+)$/);
      if (rec && CONTENT[rec[1]] && rec[2] in CONTENT[rec[1]]) {
        const body = CONTENT[rec[1]][rec[2]];
        res.setHeader("Content-Type", rec[2] === "subtitles" ? "text/vtt; charset=utf-8" : "text/plain; charset=utf-8");
        res.end(body);
        return;
      }
      if (p === `/api/tasks/${TASK}/transcript`) {
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        res.end(TASK_TRANSCRIPT);
        return;
      }
      if (TASK_CONTENT.test(p) || rec) {
        res.statusCode = 404;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ detail: "not here" }));
        return;
      }
      res.setHeader("Content-Type", "application/json");
      const table = {
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/tasks": [TASK_ROW],
        "/api/recordings": {
          // The detached one has no media left (its task is gone): its title
          // must still open the player, which then shows the transcript alone.
          items: [
            recording(ATTACHED, SOURCE_TASK, "Attached"),
            { ...recording(DETACHED, null, "Detached"), has_media: false },
          ],
          total: 2,
        },
        "/api/version": { version: "verify" },
        "/api/push/config": { enabled: false },
        "/api/prompts": [],
        [`/api/tasks/${SOURCE_TASK}/speaker-matches`]: [],
      };
      res.end(JSON.stringify(p in table ? table[p] : {}));
      return;
    }
    const f = p === "/" ? "/index.html" : p.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    if (ext === ".woff2" || ext === ".woff" || ext === ".png") { res.end(fs.readFileSync(fp)); return; }
    res.setHeader("Content-Type", CT[ext] || "text/plain");
    res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
  });
  await new Promise((r) => server.listen(0, r));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const browser = await launch();

  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#main-tab-library", { timeout: 8000 });
    await page.click("#main-tab-library");

    const card = (rid) => `#library-list [data-recording-id="${rid}"]`;
    const loaded = await page.waitForSelector(card(DETACHED), { timeout: 8000 })
      .then(() => true).catch(() => false);
    if (!loaded) {
      failures.push("the library never rendered the two recordings — nothing below proves anything");
      return failures;
    }

    // Wait for a panel to hold exactly `want`; report what it held instead.
    const panelText = (sel, tab) => page.evaluate(([s, t]) => {
      const el = document.querySelector(s);
      return el ? (el.querySelector(`.tab-content.${t}`)?.textContent || "") : null;
    }, [sel, tab]);
    const expectPanel = async (sel, tab, want, label) => {
      const ok = await page.waitForFunction(([s, t, w]) => {
        const el = document.querySelector(s);
        return (el?.querySelector(`.tab-content.${t}`)?.textContent || "") === w;
      }, [sel, tab, want], { timeout: 4000 }).then(() => true).catch(() => false);
      if (!ok) {
        failures.push(`${label}: the ${tab} panel shows ${JSON.stringify(await panelText(sel, tab))}, expected ${JSON.stringify(want)}`);
      }
    };
    const clickTab = (sel, tab) => page.click(`${sel} .tab-btn[data-tab="${tab}"]`);

    for (const [rid, label] of [[ATTACHED, "attached"], [DETACHED, "detached"]]) {
      const sel = card(rid);
      const c = CONTENT[rid];
      await page.click(`${sel} .toggle-btn`);
      await expectPanel(sel, "transcript", c["text/raw"], label);

      await clickTab(sel, "redacted");
      await expectPanel(sel, "redacted", c["text/redacted"], label);

      await clickTab(sel, "summary");
      await expectPanel(sel, "summary", c["text/summary"], label);

      const memoTab = await page.evaluate((s) =>
        document.querySelector(s)?.querySelector(".tab-btn[data-prompt-id]")?.dataset.tab || "", sel);
      if (!memoTab) {
        failures.push(`${label}: no Memo tab for the recording's prompt result`);
      } else {
        await clickTab(sel, memoTab);
        await expectPanel(sel, memoTab, c[`results/user/${MEMO}`], label);
      }

      // Subtitles: the same transcript tab, the other view.
      await clickTab(sel, "transcript");
      await expectPanel(sel, "transcript", c["text/raw"], label);
      await page.click(`${sel} .tab-subtitles-btn`);
      await expectPanel(sel, "transcript", c.subtitles, `${label} (subtitles view)`);
      await page.click(`${sel} .tab-subtitles-btn`);
      await expectPanel(sel, "transcript", c["text/raw"], `${label} (back to text)`);

      // The player is opened by the recording, not by a task that may be gone.
      const href = await page.evaluate((s) =>
        document.querySelector(s)?.querySelector(".task-link")?.getAttribute("href") || "", sel);
      if (!href.endsWith(`/player/${rid}`)) {
        failures.push(`${label}: the title links ${JSON.stringify(href)}, expected /player/${rid}`);
      }

      // Share: the plain transcript while the tab shows subtitles, and the
      // subtitles file — both read straight from the recording.
      await page.click(`${sel} .tab-subtitles-btn`);
      await expectPanel(sel, "transcript", c.subtitles, `${label} (subtitles before share)`);
      const before = requests.length;
      await page.click(`${sel} .tab-share-btn`);
      await page.waitForSelector("#share-dialog[open]", { timeout: 3000 }).catch(() => {});
      for (const value of ["transcript", "transcript:subtitles"]) {
        await page.check(`#share-dialog input[name="share-choice"][value="${value}"]`, { timeout: 2000 }).catch(() => {});
      }
      const wanted = [`/api/recordings/${rid}/text/raw`, `/api/recordings/${rid}/subtitles`];
      let shareOk = false;
      for (let i = 0; i < 60 && !shareOk; i += 1) {
        const seen = requests.slice(before);
        shareOk = wanted.every((w) => seen.includes(w));
        if (!shareOk) await page.waitForTimeout(50);
      }
      if (!shareOk) {
        failures.push(`${label}: the share dialog did not read both files from the recording; it requested ${JSON.stringify(requests.slice(before))}`);
      }
      await page.keyboard.press("Escape");
      await page.waitForSelector("#share-dialog[open]", { state: "detached", timeout: 2000 }).catch(() => {});
      await page.click(`${sel} .tab-subtitles-btn`);
    }

    // The detached card offers nothing that acts on its (gone) task.
    const detachedControls = await page.evaluate((s) => {
      const el = document.querySelector(s);
      const shown = (q) => {
        const n = el?.querySelector(q);
        return n ? getComputedStyle(n).display !== "none" : false;
      };
      return {
        speakerBox: shown(".speaker-box"),
        restart: shown(".restart-summary-btn"),
        restartFull: shown(".restart-summary-full-btn"),
        restartFinal: shown(".restart-summary-final-btn"),
        log: shown('.tab-btn[data-tab="log"]'),
      };
    }, card(DETACHED));
    for (const [what, shown] of Object.entries(detachedControls)) {
      if (shown) failures.push(`detached: ${what} is offered for a recording whose task is gone`);
    }

    // Setup guard for the request checks below: the attached card's speakers
    // still come from its source task, which proves the log is being filled.
    if (!requests.includes(`/api/tasks/${SOURCE_TASK}/speaker-matches`)) {
      failures.push("attached: the speaker panel did not load from the source task");
    }
    const viaTask = requests.filter((p) => TASK_CONTENT.test(p));
    if (viaTask.length) {
      failures.push(`library content was read through task routes: ${JSON.stringify([...new Set(viaTask)])}`);
    }
    const toDetached = requests.filter((p) => p.startsWith(`/api/tasks/${DETACHED}`));
    if (toDetached.length) {
      failures.push(`the detached card called task routes with its recording id: ${JSON.stringify([...new Set(toDetached)])}`);
    }

    // The task card is unchanged: it reads the task route.
    await page.click("#main-tab-tasks");
    const taskSel = `#task-list [data-task-id="${TASK}"]`;
    const taskShown = await page.waitForSelector(taskSel, { timeout: 5000 }).then(() => true).catch(() => false);
    if (!taskShown) {
      failures.push("the task card did not render");
    } else {
      await page.click(`${taskSel} .toggle-btn`);
      await expectPanel(taskSel, "transcript", TASK_TRANSCRIPT, "task card");
      const recordingCalls = requests.filter((p) => p.startsWith(`/api/recordings/${TASK}`));
      if (recordingCalls.length) {
        failures.push(`the task card read recording routes: ${JSON.stringify(recordingCalls)}`);
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

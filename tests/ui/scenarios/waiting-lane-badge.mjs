// VOS-85 / vts-rhs: a task in the new `waiting` status must render a badge
// showing the lane and its position, e.g. "waiting: GPU (#1)" (en),
// "ждёт: GPU (№1)" (ru), "wartet: GPU (Nr. 1)" (de). A `queued` task must keep
// the old global-position badge ("queue #N" / "очередь #N"). Black-box: render
// real task cards via the stub and read the real `.task-status` textContent,
// switching the browser locale (navigator.language) per case so the app's own
// detectLocale()/i18n path produces the text — no white-box calls.
import { startStubServer, launch } from "../harness.mjs";

const WAITING_GPU = {
  id: "aaaaaaaa-0000-0000-0000-000000000001",
  source_url: "http://x/gpu", source_title: "GPU waiter",
  status: "waiting", queue: "gpu", queue_position: 1,
  options: { transcript: true, prompts: [{ source: "system", id: "summary" }] },
  steps: [{ name: "download", status: "completed" },
          { name: "transcribe_segments", status: "completed" }],
  created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:01:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 0, total: 1 } }, stats: {},
};
const WAITING_NETWORK = {
  ...WAITING_GPU,
  id: "aaaaaaaa-0000-0000-0000-000000000002",
  source_title: "Net waiter", queue: "network", queue_position: 2,
  steps: [{ name: "download", status: "pending" }],
};
const WAITING_FFMPEG = {
  ...WAITING_GPU,
  id: "aaaaaaaa-0000-0000-0000-000000000003",
  source_title: "FFmpeg waiter", queue: "ffmpeg", queue_position: 1,
  steps: [{ name: "extract_audio", status: "pending" }],
};
const QUEUED = {
  ...WAITING_GPU,
  id: "aaaaaaaa-0000-0000-0000-000000000004",
  source_title: "Queued", status: "queued", queue: null, queue_position: 3,
  steps: [],
};

const TASKS = [WAITING_GPU, WAITING_NETWORK, WAITING_FFMPEG, QUEUED];

// Per-locale expectations. Each entry: the substrings that MUST appear in the
// given task's status badge. We assert on the lane word + position marker so
// the check is robust to surrounding phrasing.
const EXPECT = {
  en: {
    [WAITING_GPU.id]: ["GPU", "#1"],
    [WAITING_NETWORK.id]: ["download", "#2"],
    [WAITING_FFMPEG.id]: ["conversion", "#1"],
    [QUEUED.id]: ["#3"],
  },
  ru: {
    [WAITING_GPU.id]: ["GPU", "№1"],
    [WAITING_NETWORK.id]: ["скачивание", "№2"],
    [WAITING_FFMPEG.id]: ["конвертация", "№1"],
    // queued_pos is a pre-existing key that uses "#{position}" in every locale
    // (only the new waiting_pos localizes the № / Nr. marker). Assert reality.
    [QUEUED.id]: ["#3"],
  },
  de: {
    [WAITING_GPU.id]: ["GPU", "Nr. 1"],
    [WAITING_NETWORK.id]: ["Download", "Nr. 2"],
    [WAITING_FFMPEG.id]: ["Konvertierung", "Nr. 1"],
    [QUEUED.id]: ["#3"],
  },
};

export const name = "waiting-lane-badge";

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const browser = await launch();
  const failures = [];
  try {
    for (const locale of ["en", "ru", "de"]) {
      const context = await browser.newContext({ viewport: { width: 1100, height: 700 }, locale });
      const page = await context.newPage();
      page.on("pageerror", (e) => failures.push(`[${locale}] pageerror: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error" && !m.text().includes("EventSource")) {
          failures.push(`[${locale}] console.error: ${m.text()}`);
        }
      });
      await page.goto(baseUrl, { waitUntil: "networkidle" });
      await page.waitForTimeout(400);

      // Sanity: the app actually resolved to the requested locale.
      const active = await page.evaluate(() => (window.state ? window.state.locale : null));
      // window.state may not be exposed; fall back to reading a known key.
      // Not fatal — the badge-text assertions below are the real gate.

      const rows = await page.$$(".task");
      if (rows.length !== TASKS.length) {
        failures.push(`[${locale}] expected ${TASKS.length} task rows, got ${rows.length}`);
        await context.close();
        continue;
      }

      for (const task of TASKS) {
        const badge = await page.evaluate((id) => {
          const el = document.querySelector(`.task[data-task-id="${id}"] .task-status`)
            || [...document.querySelectorAll(".task")]
                 .map((t) => t._runtime && t._runtime.task && t._runtime.task.id === id
                       ? t.querySelector(".task-status") : null)
                 .find(Boolean);
          return el ? el.textContent.trim() : null;
        }, task.id);

        if (badge === null) {
          failures.push(`[${locale}] no .task-status for task ${task.source_title}`);
          continue;
        }
        for (const needle of EXPECT[locale][task.id]) {
          if (!badge.includes(needle)) {
            failures.push(`[${locale}] ${task.source_title}: badge "${badge}" missing "${needle}"`);
          }
        }
        // Guard against undefined/null leaking into the text.
        if (/undefined|null|NaN|#null|№null/.test(badge)) {
          failures.push(`[${locale}] ${task.source_title}: badge leaks placeholder: "${badge}"`);
        }
      }

      // Waiting badge must carry the distinct status-waiting class (not queued).
      const gpuClass = await page.evaluate((id) => {
        const rows = [...document.querySelectorAll(".task")];
        for (const t of rows) {
          const s = t.querySelector(".task-status");
          if (s && s.textContent.includes("1") &&
              (s.className.includes("status-waiting") || s.className.includes("status-queued"))) {
            // find the specific gpu waiter by its title
            const title = t.textContent || "";
            if (title.includes("GPU waiter")) return s.className;
          }
        }
        return null;
      }, WAITING_GPU.id);
      if (gpuClass && !gpuClass.includes("status-waiting")) {
        failures.push(`[${locale}] gpu waiter badge class is "${gpuClass}", expected status-waiting`);
      }

      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

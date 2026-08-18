// Redesign v2 of the About dialog, plus the card compaction that came with it.
//
// Two linked changes, both from Victor's review of the shipped build:
//  1. The card's clickable "stats" pill (duration · size) is gone. It cost every
//     card a second line and a box for information that is only ever read, so
//     the numbers moved onto the source line as plain text and About moved to
//     the kebab menu. The card is one line shorter for it.
//  2. The dialog itself: the task's NAME is the heading (not a generic "About
//     task"), the facts are ONE flat key/value table instead of three sections
//     that each spent a heading on two rows, and the pipeline steps are listed
//     with their outcome and duration.
//
// The steps list is the part that carries new information — "where did the time
// go" previously meant reading the log tab.
import { startStubServer, launch, openPage, dialogOpen, openTaskAbout } from "../harness.mjs";

export const name = "about-dialog-redesign";

const ISO = "2026-08-17T10:00:00Z";
const step = (name, status, a, b) => ({ name, status, started_at: a, finished_at: b });

const TASK = {
  id: "a1111111-1111-1111-1111-111111111111",
  status: "completed",
  source_url: "https://youtube.com/watch?v=k3Xp9",
  source_title: "Quarterly planning call",
  created_at: ISO, updated_at: ISO,
  media_path: "/m.mp4", transcript_path: "/t.txt", summary_path: "/s.md",
  options: {
    transcript: true, diarize: true, language: "",
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  stats: { media_seconds: 3860, media_bytes: 176000000, processing_seconds: 453 },
  steps: [
    // Supplied ALPHABETICALLY on purpose: that is the order the API serves them
    // in (serialization.py sorts on item.name), and rendering that order made the
    // dialog read as if the pipeline ran language-detection before the download.
    step("detect_language", "completed", "2026-08-17T10:07:39Z", "2026-08-17T10:08:00Z"),
    step("download", "completed", "2026-08-17T10:00:00Z", "2026-08-17T10:04:13Z"),
    step("extract_audio", "completed", "2026-08-17T10:04:13Z", "2026-08-17T10:07:39Z"),
    step("summarize_final", "completed", "2026-08-17T10:11:00Z", "2026-08-17T10:13:27Z"),
    step("transcribe_segments", "completed", "2026-08-17T10:08:00Z", "2026-08-17T10:11:00Z"),
  ],
};

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl, { width: 1200, height: 900 });

    // ---- 1. The card ----
    // The pill is gone: no bordered, clickable stats control on the card.
    const pill = await page.$$eval(".task .task-stats-chip", (els) => els.length);
    if (pill) failures.push(`the stats pill is back on the card (${pill} found)`);

    // The numbers are still there, as plain text on the source line.
    const meta = await page.evaluate(() => {
      const stats = document.querySelector(".task .task-stats");
      const src = document.querySelector(".task .task-source");
      if (!stats || !src) return null;
      const sr = stats.getBoundingClientRect();
      const rr = src.getBoundingClientRect();
      return {
        text: stats.textContent.trim(),
        hidden: stats.classList.contains("hidden"),
        sameLine: Math.abs(sr.top - rr.top) < 6,
        // A button would mean the pill came back in another form.
        isButton: stats.tagName === "BUTTON",
      };
    });
    if (!meta) failures.push("no .task-stats / .task-source on the card");
    else {
      if (meta.hidden || !meta.text) failures.push("duration/size vanished from the card entirely");
      if (!meta.sameLine) failures.push("stats are not on the same line as the source url");
      if (meta.isButton) failures.push("stats are a button again (should be plain text)");
    }

    // ---- 2. The dialog ----
    await openTaskAbout(page);
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About did not open from the kebab menu — it is the only way in now");
      return failures;
    }

    const dlg = await page.evaluate(() => {
      const d = document.getElementById("task-about-dialog");
      const q = (s) => d.querySelector(s);
      const rows = [...d.querySelectorAll(".about-facts .about-row")].filter(
        (r) => !r.classList.contains("hidden")
      );
      const steps = [...d.querySelectorAll(".about-step-row")].map((r) => ({
        name: r.querySelector(".about-step-name")?.textContent || "",
        state: r.querySelector(".about-step-state")?.textContent || "",
        time: r.querySelector(".about-step-time")?.textContent || "",
        dotted: !!r.querySelector(".about-step-dot"),
      }));
      return {
        heading: (q(".about-source-title .task-link-text")?.textContent || "").trim(),
        // Facts that only exist after the redesign.
        id: (q(".about-id")?.textContent || "").trim(),
        status: (q(".about-status")?.textContent || "").trim(),
        sourceType: (q(".about-source-type")?.textContent || "").trim(),
        media: (q(".about-media")?.textContent || "").trim(),
        factRows: rows.length,
        steps,
        stepsHidden: q(".about-steps-section")?.classList.contains("hidden"),
      };
    });

    // The heading is the task, not a generic label.
    if (dlg.heading !== "Quarterly planning call") {
      failures.push(`heading should be the task name, got ${JSON.stringify(dlg.heading)}`);
    }
    for (const [k, v] of Object.entries({
      id: dlg.id, status: dlg.status, sourceType: dlg.sourceType, media: dlg.media,
    })) {
      if (!v) failures.push(`the "${k}" fact row is empty`);
    }
    // Duration/size live here now that the card no longer shows a pill.
    if (!/1:04:20/.test(dlg.media)) {
      failures.push(`the media row should carry the duration, got ${JSON.stringify(dlg.media)}`);
    }
    if (dlg.factRows < 8) failures.push(`expected the flat fact table, got ${dlg.factRows} rows`);

    // The steps list: every step named, marked and timed.
    if (dlg.stepsHidden) failures.push("the pipeline steps section is hidden for a task that has steps");
    if (dlg.steps.length !== TASK.steps.length) {
      failures.push(`expected ${TASK.steps.length} step rows, got ${dlg.steps.length}`);
    }
    for (const s of dlg.steps) {
      if (!s.dotted) failures.push(`step "${s.name}" has no status dot`);
      if (!s.state) failures.push(`step "${s.name}" has no outcome`);
      // Raw step keys mean the i18n lookup fell through.
      if (/^\d+\.\s*(download|extract_audio|transcribe_segments|summarize_final)$/.test(s.name)) {
        failures.push(`step name is untranslated: ${JSON.stringify(s.name)}`);
      }
    }
    // Each of these steps ran to completion, so each must report how long it took.
    // Steps must read in PIPELINE order, not the alphabetical order the API
    // serves them in. The fixture above is deliberately alphabetical, so this
    // fails unless the dialog re-orders. Asserted by position of known steps
    // rather than by a full expected list, so adding a step to the pipeline
    // does not break it.
    const names = dlg.steps.map((s) => s.name);
    const at = (needle) => names.findIndex((n) => n.toLowerCase().includes(needle));
    const download = at("download");
    const extract = at("extraction");
    const transcribe = at("transcription");
    const summary = at("final summary");
    if (download < 0 || extract < 0 || transcribe < 0 || summary < 0) {
      failures.push(`could not locate the known steps in ${JSON.stringify(names)}`);
    } else if (!(download < extract && extract < transcribe && transcribe < summary)) {
      failures.push(
        `pipeline steps are out of order — download/extract/transcribe/summary ` +
        `landed at ${download}/${extract}/${transcribe}/${summary}: ${JSON.stringify(names)}`
      );
    }

    const untimed = dlg.steps.filter((s) => !s.time);
    if (untimed.length) {
      failures.push(`completed steps with no duration: ${JSON.stringify(untimed.map((s) => s.name))}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

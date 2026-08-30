// vts-lib2: Tasks and Library as two views of the main screen.
//
// Tasks are JOBS — they run, fail, get restarted. Recordings are what those
// jobs produced and outlive them. The Library reuses the task card so a
// recording's artifacts (transcript, summary, prompt results) are reachable
// the same way, with the job-shaped parts removed rather than reimplemented.
//
// What this pins is exactly that boundary: the same card, minus the things
// that would be lying about a recording.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "main-tabs-library";

const TASK_ID = "55555555-5555-5555-5555-555555555555";
const REC_ID = "66666666-6666-6666-6666-666666666666";

const TASK = {
  id: TASK_ID, source_url: "https://example.com/v", source_title: "A running job",
  status: "running", awaiting_step: null, queue: null, queue_position: null,
  transcript_path: null, summary_path: null, media_path: null,
  options: { transcript: true, prompts: [], prompt_results: [] },
  steps: [], capabilities: {},
  created_at: "2026-08-30T10:00:00Z", updated_at: "2026-08-30T10:05:00Z",
  progress: {}, stats: {},
};

const RECORDINGS = {
  items: [
    {
      id: REC_ID, source_task_id: TASK_ID, title: "Team sync",
      title_is_custom: false, source_url: "file://sync.m4a",
      duration_sec: 3725, language: "ru", tags: [],
      has_transcript: true, has_redacted: true, has_summary: true, has_media: false,
      prompt_results: [{ source: "user", id: "u1", name: "Memo", status: "completed" }],
      recorded_at: "2026-08-20T10:00:00Z",
      created_at: "2026-08-20T10:00:00Z", updated_at: "2026-08-20T11:00:00Z",
    },
  ],
  total: 1,
};

const HITS = {
  query: "найм", threshold: 0.45,
  hits: [{
    chunk_id: "77777777-7777-7777-7777-777777777777",
    recording_id: REC_ID, source_task_id: TASK_ID, title: "Team sync",
    text: "про найм говорили в самом начале", start_sec: 754.0, end_sec: 800.0,
    speakers: ["SPEAKER_00"], score: 0.556,
  }],
};

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK],
    "/api/recordings": RECORDINGS,
    "/api/search": HITS,
    // Addressed by RECORDING, not by task — that is what keeps a citation
    // working after the job is deleted.
    [`/api/recordings/${REC_ID}/transcript`]: {
      recording_id: REC_ID, title: "Team sync", variant: "raw",
      content: "", around_sec: 754.0,
      entries: [
        { start_sec: 740.0, end_sec: 752.0, text: "до этого обсуждали план", speaker: "SPEAKER_00" },
        { start_sec: 754.0, end_sec: 800.0, text: "про найм говорили в самом начале", speaker: "SPEAKER_00" },
        { start_sec: 800.0, end_sec: 812.0, text: "и дальше про сроки", speaker: "SPEAKER_01" },
      ],
    },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector("#main-tab-library", { timeout: 5000 });

    // 1. Tasks is the default view, and the New Task panel lives in it.
    if (!(await isVisible(page, "#task-form"))) {
      failures.push("the New Task form is not visible in the default view");
    }
    if (await isVisible(page, "#library-list")) {
      failures.push("the library is visible before its tab was selected");
    }

    // 2. Switching hides the task view entirely, New Task panel included.
    await page.click("#main-tab-library");
    await page.waitForTimeout(400);
    if (await isVisible(page, "#task-form")) {
      failures.push("the New Task form is still shown in the Library view");
    }
    if (!(await isVisible(page, "#library-list"))) {
      failures.push("the library list did not appear");
    }

    // 2b. The search box must actually look like a search box. Every
    //     .filter-search rule is scoped to .task-filters, so reusing the markup
    //     under .library-filters left the icon with NO width or height — an
    //     unconstrained SVG fills its container, which rendered as a huge black
    //     circle over the list.
    const icon = await page.evaluate(() => {
      const el = document.querySelector("#view-library .filter-search > svg");
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) };
    });
    if (!icon) {
      failures.push("the library search box has no icon at all");
    } else if (icon.w > 24 || icon.h > 24 || icon.w < 8) {
      // It renders at 15x15, the same as the one in the Tasks filters. The
      // bound is tight on purpose: the bug was 1068x1068, but anything that
      // drifts far from the sibling icon is already wrong.
      failures.push(
        `the library search icon is ${icon.w}x${icon.h}px, expected about 15x15`
      );
    }
    // Reported: the filter row was one short text field with no date range.
    const filters = await page.evaluate(() => ({
      from: Boolean(document.querySelector("#library-from")),
      to: Boolean(document.querySelector("#library-to")),
      searchBtn: Boolean(document.querySelector("#library-search-btn")),
    }));
    if (!filters.from || !filters.to) {
      failures.push("the library has no date-range filter, unlike the task list");
    }
    if (!filters.searchBtn) {
      failures.push("no search button — nothing hints that Enter searches content");
    }

    const box = await page.evaluate(() => {
      const el = document.querySelector("#view-library .filter-search");
      const r = el.getBoundingClientRect();
      return { h: Math.round(r.height), display: getComputedStyle(el).display };
    });
    if (box.h > 80) {
      failures.push(`the library search box is ${box.h}px tall — it is not laid out`);
    }

    await page.waitForFunction(
      () => document.querySelectorAll("#library-list .task").length > 0,
      null, { timeout: 5000 },
    ).catch(() => {});

    const cards = await page.$$eval("#library-list .task", (els) => els.length);
    if (cards !== 1) {
      failures.push(`expected 1 recording card, got ${cards}`);
      return failures;
    }

    // 3. It is a real task card — same tabs, same menu — so the artifacts are
    //    reachable the way they are for a task.
    const shape = await page.evaluate(() => {
      const card = document.querySelector("#library-list .task");
      const vis = (sel) => {
        const el = card.querySelector(sel);
        return el ? getComputedStyle(el).display !== "none" : false;
      };
      return {
        kind: card.dataset.cardKind,
        hasTabs: Boolean(card.querySelector(".tab-btn[data-tab='transcript']")),
        hasMenu: Boolean(card.querySelector(".task-menu-btn")),
        logTab: vis(".tab-btn[data-tab='log']"),
        status: vis(".task-status"),
        progress: vis(".progress-group"),
        pause: vis(".pause-btn"),
        restart: vis(".restart-summary-btn"),
        meta: card.querySelector(".library-meta")?.textContent || "",
        // Reported: every library row claimed "media deleted", the pipeline
        // step caption showed, the processed-transcript tab was dead even
        // where the artifact exists, no prompt tab appeared, and the menu
        // offered player/archive/download — all of which belong to a task.
        expired: vis(".task-expired, [data-expired]"),
        stepCaption: vis(".progress-caption"),
        redactedTab: Boolean(card.querySelector(".tab-btn[data-tab='redacted']:not([disabled])")),
        promptTab: Boolean(card.querySelector(".tab-btn[data-prompt-id]")),
        playerItem: vis(".task-player-btn"),
        archiveItem: vis(".archive-btn"),
        downloadItem: vis(".download-media-btn"),
      };
    });

    // A RECORDING is the base object; a TASK is a recording plus a job. The
    // marker names which one the card is showing.
    if (shape.kind !== "recording") {
      failures.push(`the card is not marked as a recording (got ${JSON.stringify(shape.kind)})`);
    }
    if (!shape.hasTabs) failures.push("a recording card has no transcript tab — artifacts unreachable");
    if (!shape.hasMenu) failures.push("a recording card has no menu");

    // 4. …minus everything that belongs to a JOB.
    for (const [what, shown] of Object.entries({
      "the log tab": shape.logTab,
      "the status": shape.status,
      "the progress bar": shape.progress,
      "the pause button": shape.pause,
      "the restart-summary action": shape.restart,
      "the pipeline step caption": shape.stepCaption,
      "the 'media deleted' note": shape.expired,
      "the open-player menu item": shape.playerItem,
      "the archive menu item": shape.archiveItem,
      "the download-media menu item": shape.downloadItem,
    })) {
      if (shown) failures.push(`${what} is shown on a recording, where it means nothing`);
    }

    // …and what a recording DOES have must be reachable.
    if (!shape.redactedTab) {
      failures.push("the processed-transcript tab is dead for a recording that has one");
    }
    if (!shape.promptTab) {
      failures.push("a user prompt's result has no tab in the library");
    }

    // 5. Duration and language are what a library row is scanned by.
    if (!/1:02:05/.test(shape.meta) || !/RU/.test(shape.meta)) {
      failures.push(`the library meta line is missing duration or language: ${JSON.stringify(shape.meta)}`);
    }

    // 6. Content search: Enter runs it, and a hit offers a deep link.
    await page.click("#library-q");
    await page.type("#library-q", "найм");
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-hits-list .library-hit").length > 0,
      null, { timeout: 5000 },
    ).catch(() => {});
    const hit = await page.evaluate(() => {
      const el = document.querySelector("#library-hits-list .library-hit");
      if (!el) return null;
      return {
        text: el.querySelector(".library-hit-text")?.textContent || "",
        // A citation must not depend on the TASK: /player/{task} 404s once the
        // task is deleted, and a library result is about the recording.
        playerLink: Boolean(el.querySelector("a[href*='/player/']")),
        hasExpand: Boolean(el.querySelector(".library-hit-expand")),
        // Two ways to follow one hit, so they read as a pair of equals.
        bothAreIcons: Boolean(el.querySelector(".library-hit-actions .icon-btn.library-hit-expand"))
          && Boolean(el.querySelector(".library-hit-actions a.icon-btn")),
        // Following a citation must not throw away the search that found it.
        opensInNewTab: el.querySelector("a[href*='/player/']")?.getAttribute("target") === "_blank",
        tooltips: Boolean(el.querySelector(".library-hit-expand")?.getAttribute("title"))
          && Boolean(el.querySelector("a[href*='/player/']")?.getAttribute("title")),
      };
    });
    if (!hit) {
      failures.push("pressing Enter did not run a content search");
    } else {
      if (!hit.text.includes("найм")) failures.push("the hit does not show the passage");
      // Both ways to follow a hit, because they serve different needs: the
      // player is for a person to watch, the expander is for reading the
      // passage — and only the second survives the task's deletion.
      if (!hit.playerLink) {
        failures.push("no player link on a hit whose task is alive");
      }
      if (!hit.hasExpand) failures.push("no way to see the passage in its transcript");
      if (!hit.bothAreIcons) {
        failures.push("the two hit actions are not a matching pair of icon buttons");
      }
      if (!hit.opensInNewTab) {
        failures.push("the player link replaces the page, losing the search behind it");
      }
      if (!hit.tooltips) failures.push("an icon button with no tooltip says nothing");
    }

    // 7. Expanding reads the passage from the RECORDING, and marks the quoted
    //    line inside its context.
    await page.click("#library-hits-list .library-hit-expand");
    await page.waitForFunction(
      () => document.querySelectorAll("#library-hits-list .library-context-line").length > 0,
      null, { timeout: 5000 },
    ).catch(() => {});
    const context = await page.evaluate(() => {
      const lines = [...document.querySelectorAll("#library-hits-list .library-context-line")];
      return {
        count: lines.length,
        marked: lines.filter((l) => l.classList.contains("is-hit")).length,
        text: lines.map((l) => l.textContent).join(" | "),
      };
    });
    if (!context.count) {
      failures.push("expanding a hit showed no transcript context");
    } else {
      if (!context.marked) {
        failures.push("the quoted line is not marked inside its context");
      }
      if (!context.text.includes("найм")) {
        failures.push(`the context does not contain the quoted passage: ${context.text}`);
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Victor's review pass on the shipped 1.7.19 build, all of it about making the
// list dense enough to scan. Grouped in one scenario because they are one
// change of intent, and each half is cheap to assert:
//
//  1. Task card controls on ONE row (the chevron used to sit above the rest),
//     with a fixed-width clock column so nothing shifts as a task runs.
//  2. Artefact sizes as chips at the bottom of the card, appearing as each one
//     lands — NOT one chip per user prompt, which would grow without bound.
//  3. A count beside the "Tasks" heading, from the server rather than from
//     counting rendered cards (the list is paginated).
//  4. A "Load more" button. Infinite scroll stays; the button is what makes the
//     next page reachable when the list is too short to scroll at all — which
//     compact cards make ordinary.
//  5. New-task row: no "Preset"/"Prompts" captions, a divider after the preset.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "card-and-form-compaction";

// Mirrors vts.services.task_status.status_flags() — only the fields the card
// reads. `running` is pausable and `paused`/`failed` resumable, so the fixture
// below produces cards with DIFFERING button counts, which is what makes the
// chip-alignment assertion meaningful.
const FLAGS = {
  running:   { is_active: true,  is_pending: false, is_finished: false, shows_progress: true,  can_pause: true,  can_resume: false, can_archive: false },
  paused:    { is_active: false, is_pending: false, is_finished: false, shows_progress: false, can_pause: false, can_resume: true,  can_archive: false },
  completed: { is_active: false, is_pending: false, is_finished: true,  shows_progress: true,  can_pause: false, can_resume: false, can_archive: true  },
  failed:    { is_active: false, is_pending: false, is_finished: true,  shows_progress: true,  can_pause: false, can_resume: true,  can_archive: true  },
};

const iso = new Date().toISOString();
const card = (i, extra = {}) => ({
  id: `t${i}`,
  status: "completed",
  source_url: `https://youtube.com/watch?v=v${i}`,
  source_title: `Task ${i}`,
  display_name: `Task ${i}`,
  created_at: new Date(Date.now() - i * 60000).toISOString(),
  updated_at: iso,
  media_path: "/m.mp4",
  options: { transcript: true, prompts: [] },
  steps: [],
  ...extra,
});

// A full page back means "there may be more" — that is what shows Load more.
const TASKS = [
  card(0, {
    status: "running",
    steps: [{ name: "transcribe_segments", status: "running", started_at: new Date(Date.now() - 252000).toISOString() }],
    stats: { transcript_chars: 18240, redacted_chars: 15980, summary_chars: 2310 },
  }),
  card(1, { stats: { transcript_chars: 4120 } }),   // only the raw transcript exists yet
  card(2),                                          // nothing produced yet
  // Still RUNNING and has no media yet — must NOT claim the media was deleted.
  // Keyed on "no media" alone, the note fired here too, on freshly created
  // tasks that simply had not downloaded anything yet.
  card(91, { status: "running", media_path: undefined }),
  // Finished with no media: this is the real "pruned by retention" case.
  card(92, { media_path: undefined }),
  // A resumable card: it renders the play button where cards 0 and 2 render
  // none, so the chip-alignment check sees both states.
  card(90, { status: "paused" }),
  ...Array.from({ length: 7 }, (_, i) => card(i + 3)),
];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": TASKS,
    "/api/tasks/count": { total: 42 },
    // Real flags, not {}: pause/resume visibility is driven by these, and with
    // an empty map no card renders that button — which would make the
    // chip-alignment check below pass against anything. Mirrors
    // vts.services.task_status.status_flags().
    "/api/status-config": { status_flags: FLAGS, tasks_page_size: 10 },
    "/api/presets": [{ source: "user", id: "p1", name: "Quick summary", editable: true,
      options: { transcript: true, prompts: [{ source: "system", id: "summary" }] } }],
    "/api/me/default_preset": { source: "user", id: "p1" },
    "/api/prompts": [
      { source: "system", id: "summary", name: "Summary", editable: false, system_prompt: "x" },
      { source: "user", id: "u1", name: "Memo", editable: true, system_prompt: "y" },
    ],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl, { width: 1150, height: 900 });
    await page.waitForTimeout(500);

    // ---- 1. One row of controls, fixed clock column ----
    const rows = await page.evaluate(() => {
      const cards = [...document.querySelectorAll(".task")];
      return cards.slice(0, 3).map((c) => {
        const mid = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
        const chip = c.querySelector(".task-status");
        const kebab = c.querySelector(".task-menu-btn");
        const toggle = c.querySelector(".toggle-btn");
        const clock = c.querySelector(".task-runtime");
        const dot = c.querySelector(".task-dot");
        const hdr = c.querySelector(".task-header-row");
        const r = (el) => { const b = el.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.right)]; };
        return {
          // Same visual line: centres within a couple of px of each other.
          sameLine: Math.abs(mid(chip) - mid(toggle)) < 3 && Math.abs(mid(kebab) - mid(toggle)) < 3,
          clockCol: r(clock).join(".."),
          kebabLeft: r(kebab)[0],
          dotOffset: Math.round(mid(dot) - mid(hdr)),
        };
      });
    });
    for (const [i, row] of rows.entries()) {
      if (!row.sameLine) failures.push(`card ${i}: status chip, kebab and chevron are not on one line`);
      if (Math.abs(row.dotOffset) > 2) failures.push(`card ${i}: status dot is ${row.dotOffset}px off the header's centre`);
    }
    // The clock column must not move between a card that has a time and one
    // that does not — that is the whole point of reserving it.
    const cols = new Set(rows.map((r) => r.clockCol));
    if (cols.size !== 1) failures.push(`the clock column shifts between cards: ${[...cols].join(" vs ")}`);
    const kebabs = new Set(rows.map((r) => r.kebabLeft));
    if (kebabs.size !== 1) failures.push(`the kebab is not in one column: ${[...kebabs].join(" vs ")}`);

    // ---- 1b. Chips line up regardless of which buttons a card shows --------
    // Pause/resume is the only control that comes and goes, so a card without
    // it used to be a button narrower and its chip sat ~41px right of its
    // neighbours'. The pause button now holds the slot open when hidden, and
    // THIS is the assertion that would have caught the drift: the earlier
    // checks all use cards in the same state, so they cannot see it.
    // Needs real status flags — pause/resume visibility is driven by
    // /api/status-config, and without them no card renders the button at all.
    const chipCols = await page.evaluate(() =>
      [...document.querySelectorAll(".task")].map((c) => ({
        status: c.querySelector(".task-status")?.textContent.trim() || "",
        right: Math.round(c.querySelector(".task-status").getBoundingClientRect().right),
        // Count only buttons the user can actually press. The reserved
        // pause slot still occupies width (that is the fix), so measuring by
        // width alone reports the same number for every card and the vacuity
        // guard below could never fire.
        buttons: [...c.querySelectorAll(".task-actions-inline button, .toggle-btn")]
          .filter((b) => {
            const cs = getComputedStyle(b);
            return b.getBoundingClientRect().width > 0
              && cs.visibility !== "hidden"
              && cs.display !== "none";
          }).length,
      }))
    );
    const varied = new Set(chipCols.map((c) => c.buttons));
    if (varied.size < 2) {
      failures.push(
        `this fixture no longer produces cards with DIFFERING button counts ` +
        `(${[...varied].join(",")}), so the chip-alignment check is vacuous`
      );
    }
    const rightEdges = new Set(chipCols.map((c) => c.right));
    if (rightEdges.size !== 1) {
      failures.push(
        `status chips are not in one column across button states: ` +
        JSON.stringify(chipCols.map((c) => `${c.status}:${c.buttons}btn@${c.right}`))
      );
    }

    // ---- 1c. "media deleted" only when it really was ----------------------
    const expired = await page.evaluate(() =>
      [...document.querySelectorAll(".task")].map((c) => ({
        status: c.querySelector(".task-status")?.textContent.trim() || "",
        hasMedia: !!c.querySelector(".task-link[href]"),
        note: !c.querySelector(".task-expired")?.classList.contains("hidden"),
      }))
    );
    for (const row of expired) {
      // A running task has nothing to say about deleted media — it may simply
      // not have downloaded it yet.
      if (row.note && row.status.toLowerCase().includes("running")) {
        failures.push(`a running task claims its media was deleted (status "${row.status}")`);
      }
      // And a task that still HAS its media must never say otherwise.
      if (row.note && row.hasMedia) {
        failures.push(`a task with playable media claims the media was deleted`);
      }
    }
    if (!expired.some((r) => r.note)) {
      failures.push("no task shows the media-deleted note — the fixture no longer covers it");
    }

    // ---- 1d. A name with no media must not pretend to be a link -----------
    // The underline lives on the inner .task-link-text, so clearing it on the
    // ANCHOR alone (which is what .task-link.expired:hover did) left the name
    // underlining on hover — it read as clickable while nothing happens on
    // click. Checked on hover, because that is the only state it appears in.
    for (const [sel, label, shouldLink] of [
      [".task .task-link.expired", "a task without media", false],
      [".task .task-link:not(.expired)", "a task with media", true],
    ]) {
      const exists = await page.$(sel);
      if (!exists) {
        failures.push(`${label}: no card in that state — this check is vacuous`);
        continue;
      }
      await page.hover(sel);
      await page.waitForTimeout(150);
      const state = await page.evaluate((s) => {
        const a = document.querySelector(s);
        const t = a.querySelector(".task-link-text");
        return {
          href: a.hasAttribute("href"),
          underlined: getComputedStyle(t).textDecorationLine.includes("underline"),
          cursor: getComputedStyle(a).cursor,
        };
      }, sel);
      await page.mouse.move(0, 0);
      if (state.href !== shouldLink) {
        failures.push(`${label}: href presence is ${state.href}, expected ${shouldLink}`);
      }
      if (state.underlined !== shouldLink) {
        failures.push(
          `${label}: underlined on hover is ${state.underlined}, expected ${shouldLink}` +
          (shouldLink ? "" : " — it reads as clickable but nothing happens on click")
        );
      }
    }

    // ---- 2. Size chips ----
    const chips = await page.evaluate(() =>
      [...document.querySelectorAll(".task")].slice(0, 3).map((c) =>
        [...c.querySelectorAll(".task-size-chip")].map((x) => x.textContent.replace(/\s+/g, " ").trim())
      )
    );
    if (chips[0].length !== 3) failures.push(`a finished task should show 3 size chips, got ${JSON.stringify(chips[0])}`);
    if (chips[1].length !== 1) failures.push(`a task with only a raw transcript should show 1 chip, got ${JSON.stringify(chips[1])}`);
    if (chips[2].length !== 0) failures.push(`a task with nothing produced should show no chips, got ${JSON.stringify(chips[2])}`);
    // The number has to be in there, not just the label.
    if (chips[1].length && !/4[,. ]?120/.test(chips[1][0])) {
      failures.push(`the size chip lost its number: ${JSON.stringify(chips[1])}`);
    }

    // ---- 3. Task count from the server, not from the DOM ----
    const count = await page.evaluate(() => {
      const el = document.getElementById("tasks-count");
      return el ? { text: el.textContent.trim(), hidden: el.classList.contains("hidden") } : null;
    });
    if (!count || count.hidden) failures.push("the task count pill is not shown next to the heading");
    // 42 is the stubbed server total; 10 is how many cards are rendered. Reading
    // "10" here would mean the count is being derived from the list.
    else if (count.text !== "42") failures.push(`task count should come from /api/tasks/count (42), got ${JSON.stringify(count.text)}`);

    // ---- 4. Load more, alongside infinite scroll ----
    const more = await page.evaluate(() => {
      const b = document.getElementById("task-load-more");
      if (!b) return null;
      return { hidden: b.hidden, display: getComputedStyle(b).display, text: b.textContent.trim() };
    });
    if (!more) failures.push("no #task-load-more button in the sentinel");
    else if (more.hidden || more.display === "none") {
      failures.push("Load more is hidden even though a full page came back (a next page may exist)");
    }
    // The observer must still be there — the button is a fallback, not a replacement.
    const sentinel = await page.evaluate(() => !!document.getElementById("task-sentinel"));
    if (!sentinel) failures.push("the infinite-scroll sentinel is gone — it must stay alongside the button");

    // ---- 5. New-task row ----
    const form = await page.evaluate(() => ({
      presetCaption: !!document.querySelector(".preset-field .preset-label"),
      presetHintMarker: !!document.querySelector(".preset-field .preset-hint"),
      promptsCaption: !!document.querySelector("#task-form .prompt-select-field .prompt-select-label"),
      divider: !!document.querySelector("#task-form .options-divider"),
      // The explanation must survive the caption's removal — it is the vts-lbgg
      // fix, and these are its only two carriers now.
      pillTip: document.querySelector("#preset-pill")?.getAttribute("data-tooltip") || "",
      selectTip: document.querySelector("#preset-select")?.getAttribute("data-tooltip") || "",
      promptPill: document.querySelector("#prompt-select .prompt-select-summary")?.textContent.trim() || "",
    }));
    if (form.presetCaption) failures.push('the "Preset" caption is back beside the pill');
    if (form.presetHintMarker) failures.push('the "?" marker is back beside the preset pill');
    if (form.promptsCaption) failures.push('the "Prompts" caption is back beside the prompt pill');
    if (!form.divider) failures.push("no divider between the preset pill and the option pills");
    if (!form.pillTip) failures.push("the preset pill lost the vts-lbgg explanation tooltip");
    if (!form.selectTip) failures.push("#preset-select lost the vts-lbgg explanation (what a screen reader reads)");
    // The pill names the prompt rather than counting it.
    if (form.promptPill !== "Summary") {
      failures.push(`the prompt pill should name the first selected prompt, got ${JSON.stringify(form.promptPill)}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

# About-Task Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "About task" dialog, opened by clicking a stats chip (ℹ + duration·size), showing source, run parameters, and results (incl. per-prompt finalize duration); remove the success-stats line from the card.

**Architecture:** Frontend-only. One global `<dialog id="task-about-dialog">` (same pattern as `presets-dialog`/`restart-final-dialog`), filled on click from the clicked task's `task` object (in render-loop closure). All data already arrives in the serialized task (`options`, `steps`, `stats`, `created_at`, `source_url`). No backend changes.

**Tech Stack:** Vanilla JS (`vts/static/app.js`, no module system, no `defer`), plain CSS (`vts/static/styles.css`), i18n dicts (`vts/static/i18n/{en,de,ru}.js`), Playwright UI scenarios (`tests/ui/`).

## Global Constraints

- Bump `vts/__init__.py` `__version__` before the final commit (current: `1.1.17` → `1.1.18`).
- app.js has no `defer`: any new `getElementById` for the dialog must run after the element exists in the DOM. The `<dialog>` is declared before `<script src="/static/app.js?v=...">` (line 630), so top-level `document.getElementById("task-about-dialog")` is safe — place it alongside the other dialog consts (e.g. near `presetsDialog`, line 3155).
- Closed `<dialog>` MUST keep UA `display:none`. Gate any flex layout on `#task-about-dialog[open]`. Add a global safety net `dialog:not([open]) { display: none; }`.
- i18n: add every new key to ALL THREE files `en.js`, `de.js`, `ru.js`. Format: `"key": "value",` inside the `window.__VTS_I18N.<lang> = { ... }` object.
- Run UI tests with: `cd tests/ui && node run.mjs` (all scenarios must stay green).
- No new backend/API code. Do not touch Python except the version bump.

---

## File Structure

- `vts/static/index.html` — turn `.task-stats` div into a chip button; add `<dialog id="task-about-dialog">` near the other dialogs (after `restart-final-dialog`, before `</body>`/`<script>`).
- `vts/static/app.js` — chip rendering (icon + text), chip click handler in the render loop, `renderTaskAboutDialog(task)`, dialog open/close wiring, `resolveTaskMessage` change, a shared results-format helper, new `_elements` entries.
- `vts/static/styles.css` — chip styles, dialog section styles, `#task-about-dialog[open]` layout, global `dialog:not([open])` safety net.
- `vts/static/i18n/{en,de,ru}.js` — new keys.
- `tests/ui/scenarios/task-about-dialog.mjs` — new Playwright scenario.
- `vts/__init__.py` — version bump.

---

## Task 1: Anti-flicker CSS safety net + chip styles

**Files:**
- Modify: `vts/static/styles.css` (append near the dialog block, after line ~1089)
- Modify: `vts/static/index.html:285` (`.task-stats` → chip button)

**Interfaces:**
- Produces: CSS classes `.task-stats-chip` (button), global `dialog:not([open]) { display:none }`. The chip is a `<button class="task-stats task-stats-chip" type="button">` with a static ℹ `<svg>` and a `<span class="task-stats-text">` for the metrics text. (`renderTaskStats` in Task 2 writes the `<span>`, not the button.)

- [ ] **Step 1: Add the global anti-flicker rule + chip CSS**

Append to `vts/static/styles.css` (after the `.tokens-dialog::backdrop` / `#restart-final-dialog[open]` block, ~line 1090):

```css
/* Anti-flicker safety net: a closed <dialog> must never render. Without this,
   any non-`none` display on a dialog (or a rule targeting it) leaks the dialog
   onto the page for a frame before JS/UA hides it. Belt-and-suspenders for the
   UA default. */
dialog:not([open]) {
  display: none;
}

/* Clickable stats chip (duration · size) that opens the About-task dialog. */
.task-stats-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.1rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: transparent;
  color: var(--ink-soft);
  font: inherit;
  font-size: 0.75rem;
  cursor: pointer;
}

.task-stats-chip:hover {
  background: var(--bg, #fff);
  color: var(--ink);
  border-color: var(--ink-soft);
}

.task-stats-chip svg {
  width: 0.85rem;
  height: 0.85rem;
  flex: 0 0 auto;
}
```

- [ ] **Step 2: Convert `.task-stats` into the chip button in index.html**

In `vts/static/index.html`, replace line 285:

```html
            <div class="task-stats hidden"></div>
```

with:

```html
            <button class="task-stats task-stats-chip hidden" type="button"
                    data-i18n-title="about.open" data-i18n-aria-label="about.open">
              <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="9" />
                <path d="M12 11v5M12 7.5h.01" />
              </svg>
              <span class="task-stats-text"></span>
            </button>
```

- [ ] **Step 3: Verify CSS/HTML load without errors**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED` (existing scenarios still green; chip not yet wired, `.task-stats` keeps `hidden` so nothing renders differently). Note: `about.open` i18n key is added in Task 5; until then the title attribute shows the raw key — acceptable mid-plan, fixed in Task 5.

- [ ] **Step 4: Commit**

```bash
git add vts/static/styles.css vts/static/index.html
git commit -m "feat(ui): stats chip button + global dialog anti-flicker rule"
```

---

## Task 2: `renderTaskStats` writes the chip text span (not the button)

**Files:**
- Modify: `vts/static/app.js:933-949` (`renderTaskStats`)
- Modify: `vts/static/app.js:1592` (`_elements.statsEl`) — add `statsTextEl`

**Interfaces:**
- Consumes: `.task-stats-chip` with child `.task-stats-text` (Task 1).
- Produces: `taskEl._elements.statsTextEl` (the `<span>`); `renderTaskStats` now sets text on the span and toggles `hidden` on the chip button (`elements.statsEl`).

- [ ] **Step 1: Add `statsTextEl` to the elements map**

In `vts/static/app.js`, after line 1592 (`statsEl: root.querySelector(".task-stats"),`) add:

```javascript
      statsTextEl: root.querySelector(".task-stats-text"),
```

- [ ] **Step 2: Update `renderTaskStats` to write the span, keep the icon static**

Replace the body tail of `renderTaskStats` (lines 947-948):

```javascript
  elements.statsEl.textContent = parts.join(" · ");
  elements.statsEl.classList.toggle("hidden", parts.length === 0);
```

with:

```javascript
  if (elements.statsTextEl) {
    elements.statsTextEl.textContent = parts.join(" · ");
  }
  elements.statsEl.classList.toggle("hidden", parts.length === 0);
```

- [ ] **Step 3: Verify existing UI tests still pass**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED` (chip text renders inside the span; visibility unchanged).

- [ ] **Step 4: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(ui): render stats text into chip span, keep info icon static"
```

---

## Task 3: The About-task dialog markup

**Files:**
- Modify: `vts/static/index.html` (add `<dialog>` after `restart-final-dialog`, which ends near line 628; insert before `<script src="/static/app.js?...">` at line 630)
- Modify: `vts/static/styles.css` (dialog section layout, gated on `[open]`)

**Interfaces:**
- Produces DOM ids/classes consumed by Task 4:
  - `#task-about-dialog` (the `<dialog class="tokens-dialog">`)
  - `#task-about-close-btn` (X button)
  - `.about-source`, `.about-params`, `.about-results` (section containers)
  - `.about-source-title`, `.about-source-url`, `.about-created` (source fields)
  - `.about-language`, `.about-audio-only`, `.about-transcript`, `.about-prompts` (param fields)
  - `.about-results-section` (the whole results block, hidden when not completed)
  - `.about-total-time`, `.about-raw-chars`, `.about-processed-chars`, `.about-summary-chars`
  - `.about-prompt-timings` (a `<tbody>` for per-prompt rows)

- [ ] **Step 1: Add the dialog markup**

In `vts/static/index.html`, immediately after the closing `</dialog>` of `restart-final-dialog` (line ~628) and before `<script ...>` (line 630), insert:

```html
    <dialog id="task-about-dialog" class="tokens-dialog">
      <div class="tokens-dialog-header">
        <h2 data-i18n="about.title">About task</h2>
        <button id="task-about-close-btn" class="icon-btn ghost" type="button"
                data-i18n-title="tokens.close" data-i18n-aria-label="tokens.close">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        </button>
      </div>

      <section class="about-source">
        <h3 data-i18n="about.section_source">Source</h3>
        <div class="about-source-title"></div>
        <div class="about-source-url mono"></div>
        <div><span data-i18n="about.created">Created</span>: <span class="about-created"></span></div>
      </section>

      <section class="about-params">
        <h3 data-i18n="about.section_params">Run parameters</h3>
        <div><span data-i18n="about.language">Language</span>: <span class="about-language"></span></div>
        <div><span data-i18n="about.audio_only">Audio only</span>: <span class="about-audio-only"></span></div>
        <div><span data-i18n="about.transcript">Transcript</span>: <span class="about-transcript"></span></div>
        <div><span data-i18n="about.prompts">Prompts</span>: <span class="about-prompts"></span></div>
      </section>

      <section class="about-results-section">
        <h3 data-i18n="about.section_results">Results</h3>
        <div><span data-i18n="about.total_time">Total time</span>: <span class="about-total-time"></span></div>
        <div><span data-i18n="about.raw_chars">Raw transcript</span>: <span class="about-raw-chars"></span></div>
        <div><span data-i18n="about.processed_chars">Processed transcript</span>: <span class="about-processed-chars"></span></div>
        <div><span data-i18n="about.summary_chars">Summary</span>: <span class="about-summary-chars"></span></div>
        <table class="about-timings">
          <thead>
            <tr>
              <th data-i18n="about.timing_prompt">Prompt</th>
              <th data-i18n="about.timing_duration">Duration</th>
            </tr>
          </thead>
          <tbody class="about-prompt-timings"></tbody>
        </table>
      </section>
    </dialog>
```

- [ ] **Step 2: Add dialog layout CSS (gated on `[open]`)**

Append to `vts/static/styles.css` (after the chip CSS from Task 1):

```css
/* About-task dialog: layout ONLY when open, so the closed dialog keeps
   UA display:none (no flicker on load). */
#task-about-dialog[open] {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  max-width: min(40rem, calc(100vw - 2rem));
  max-height: 80vh;
  overflow: auto;
}

#task-about-dialog section {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

#task-about-dialog h3 {
  margin: 0.25rem 0;
  font-size: 0.9rem;
  color: var(--ink-soft);
}

#task-about-dialog .about-source-url {
  word-break: break-all;
  font-size: 0.8rem;
}

.about-timings {
  border-collapse: collapse;
  margin-top: 0.25rem;
}

.about-timings th,
.about-timings td {
  text-align: left;
  padding: 0.15rem 0.75rem 0.15rem 0;
  font-size: 0.85rem;
}
```

- [ ] **Step 3: Verify the closed dialog does not render**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`. The new dialog has no `open` attribute, so `dialog:not([open])` + `#task-about-dialog[open]` gating keep it hidden; existing scenarios (esp. `smoke-boot`, `restart-dialog`) stay green.

- [ ] **Step 4: Commit**

```bash
git add vts/static/index.html vts/static/styles.css
git commit -m "feat(ui): About-task dialog markup + open-gated layout"
```

---

## Task 4: `renderTaskAboutDialog`, chip click, open/close wiring, `.task-message` change

**Files:**
- Modify: `vts/static/app.js` — add helper functions (near `resolveTaskMessage`, ~line 951), chip click handler (in render loop near line 1555), elements entry, dialog open/close wiring (top-level near line 3155).

**Interfaces:**
- Consumes: `promptDisplayName({source,id,name})` (line 1787), `selectedPromptRefs(options)` (line 720), `finalizeStepName(source,id)` (line 710), `parseIsoMs(value)` (line 250), `formatDuration(seconds)` (line 239), `formatMetricChars(value)` (line 905), `formatMetricDuration(seconds)` (line 912), `t(key, params)` (line 177). Dialog DOM from Task 3.
- Produces: `formatResultStats(runtime)` (returns `{time, raw, processed, summary}` strings), `renderTaskAboutDialog(task)`, `openTaskAboutDialog(task)`. `taskEl._elements.statsEl` doubles as the chip click target.

- [ ] **Step 1: Add the shared results-format helper and replace `resolveCompletedMessage` usage**

In `vts/static/app.js`, replace `resolveCompletedMessage` (lines 951-961) and `resolveTaskMessage` (lines 963-969) with:

```javascript
// Shared formatter for the completed-run numbers (total time + char counts).
// Used by the About-task dialog. Returns localized display strings.
function formatResultStats(runtime) {
  const stats = runtime.stats || {};
  return {
    time: formatMetricDuration(stats.processingSeconds),
    raw: formatMetricChars(stats.transcriptChars),
    processed: formatMetricChars(stats.redactedChars),
    summary: formatMetricChars(stats.summaryChars)
  };
}

function resolveTaskMessage(runtime) {
  // Card message line now carries ONLY the failure text. The success stats
  // moved into the About-task dialog (formatResultStats).
  return resolveFailureMessage(runtime);
}
```

- [ ] **Step 2: Add `renderTaskAboutDialog` and `openTaskAboutDialog`**

Add these functions right after `formatResultStats` (so they sit near the other render helpers, ~line 970):

```javascript
const taskAboutDialog = document.getElementById("task-about-dialog");

function aboutPromptNames(options) {
  // Prefer prompt_results (carries names); fall back to selected refs.
  const results = Array.isArray(options.prompt_results) ? options.prompt_results : null;
  const refs = results && results.length
    ? results.map((r) => ({ source: r.source, id: r.id, name: r.name }))
    : selectedPromptRefs(options).map((r) => ({ source: r.source, id: r.id, name: r.id }));
  return refs.map((r) => promptDisplayName(r));
}

function aboutPromptTimings(task) {
  // One row per selected prompt: display name + finalize-step duration.
  const options = task.options || {};
  const stepByName = {};
  (task.steps || []).forEach((s) => { if (s && s.name) stepByName[s.name] = s; });
  const refs = (Array.isArray(options.prompt_results) && options.prompt_results.length)
    ? options.prompt_results.map((r) => ({ source: r.source, id: r.id, name: r.name }))
    : selectedPromptRefs(options).map((r) => ({ source: r.source, id: r.id, name: r.id }));
  return refs.map((ref) => {
    const step = stepByName[finalizeStepName(ref.source, ref.id)];
    const start = step ? parseIsoMs(step.started_at) : null;
    const end = step ? parseIsoMs(step.finished_at) : null;
    const duration = (start !== null && end !== null && end >= start)
      ? formatDuration((end - start) / 1000)
      : "—";
    return { name: promptDisplayName(ref), duration };
  });
}

function renderTaskAboutDialog(task) {
  if (!taskAboutDialog) {
    return;
  }
  const options = task.options || {};
  const runtime = { stats: parseTaskStats(task), baseStatus: String(task.status || "") };
  const q = (sel) => taskAboutDialog.querySelector(sel);

  q(".about-source-title").textContent = task.source_title || task.source_url || "";
  q(".about-source-url").textContent = task.source_url || "";
  q(".about-created").textContent = task.created_at
    ? new Date(task.created_at).toLocaleString()
    : "";

  q(".about-language").textContent = options.language || t("about.language_auto");
  q(".about-audio-only").textContent = options.audio_only ? t("about.yes") : t("about.no");
  q(".about-transcript").textContent = options.transcript === false ? t("about.no") : t("about.yes");
  q(".about-prompts").textContent = aboutPromptNames(options).join(", ") || "—";

  const completed = String(task.status || "") === "completed";
  const resultsSection = q(".about-results-section");
  resultsSection.classList.toggle("hidden", !completed);
  if (completed) {
    const fmt = formatResultStats(runtime);
    q(".about-total-time").textContent = fmt.time;
    q(".about-raw-chars").textContent = fmt.raw;
    q(".about-processed-chars").textContent = fmt.processed;
    q(".about-summary-chars").textContent = fmt.summary;
    const tbody = q(".about-prompt-timings");
    tbody.innerHTML = "";
    aboutPromptTimings(task).forEach((row) => {
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      nameTd.textContent = row.name;
      const durTd = document.createElement("td");
      durTd.textContent = row.duration;
      tr.appendChild(nameTd);
      tr.appendChild(durTd);
      tbody.appendChild(tr);
    });
  }
}

function openTaskAboutDialog(task) {
  if (!taskAboutDialog) {
    return;
  }
  renderTaskAboutDialog(task);
  if (typeof taskAboutDialog.showModal === "function") {
    taskAboutDialog.showModal();
  } else {
    taskAboutDialog.setAttribute("open", "");
  }
}
```

- [ ] **Step 3: Wire the chip click in the render loop**

In `vts/static/app.js`, inside the per-task render loop, after the existing `deleteBtn.addEventListener(...)` line (~line 1561) and before the tab-button loop (~line 1573), add:

```javascript
    if (root._elements && root._elements.statsEl) {
      root._elements.statsEl.addEventListener("click", () => openTaskAboutDialog(task));
    }
```

(Note: `root._elements` is assigned at line 1588; this handler is registered after that assignment in source order only if it sits below it. Place this block AFTER the `root._elements = { ... }` assignment block — i.e. right after line ~1640 where the object literal closes — to guarantee `statsEl` exists. Verify by reading the closing brace of the `_elements` object before inserting.)

- [ ] **Step 4: Wire the close button (top-level, near other dialog close handlers)**

In `vts/static/app.js`, near the `presets-close-btn` handler (~line 3373), add:

```javascript
document.getElementById("task-about-close-btn")?.addEventListener("click", () => {
  taskAboutDialog?.close();
});
```

- [ ] **Step 5: Manual smoke via existing test harness boot (no assertion yet)**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`. No JS errors on boot (the `console.error`/`pageerror` listeners in `smoke-boot` would catch a ReferenceError from the new code). The dialog stays closed.

- [ ] **Step 6: Commit**

```bash
git add vts/static/app.js
git commit -m "feat(ui): About-task dialog render + chip open + move success stats out of card"
```

---

## Task 5: i18n keys (en, de, ru)

**Files:**
- Modify: `vts/static/i18n/en.js`, `vts/static/i18n/de.js`, `vts/static/i18n/ru.js`

**Interfaces:**
- Consumes: keys referenced in Tasks 1, 3, 4: `about.open`, `about.title`, `about.section_source`, `about.created`, `about.section_params`, `about.language`, `about.audio_only`, `about.transcript`, `about.prompts`, `about.section_results`, `about.total_time`, `about.raw_chars`, `about.processed_chars`, `about.summary_chars`, `about.timing_prompt`, `about.timing_duration`, `about.language_auto`, `about.yes`, `about.no`.

- [ ] **Step 1: Add keys to `en.js`**

Insert into `vts/static/i18n/en.js` (e.g. after the `"results.pending"` line, line 118):

```javascript
"about.open": "About task",
"about.title": "About task",
"about.section_source": "Source",
"about.created": "Created",
"about.section_params": "Run parameters",
"about.language": "Language",
"about.language_auto": "auto",
"about.audio_only": "Audio only",
"about.transcript": "Transcript",
"about.prompts": "Prompts",
"about.yes": "yes",
"about.no": "no",
"about.section_results": "Results",
"about.total_time": "Total time",
"about.raw_chars": "Raw transcript",
"about.processed_chars": "Processed transcript",
"about.summary_chars": "Summary",
"about.timing_prompt": "Prompt",
"about.timing_duration": "Duration",
```

- [ ] **Step 2: Add keys to `de.js`**

Insert into `vts/static/i18n/de.js` (after `"results.pending"`):

```javascript
"about.open": "Über die Aufgabe",
"about.title": "Über die Aufgabe",
"about.section_source": "Quelle",
"about.created": "Erstellt",
"about.section_params": "Startparameter",
"about.language": "Sprache",
"about.language_auto": "automatisch",
"about.audio_only": "Nur Audio",
"about.transcript": "Transkript",
"about.prompts": "Prompts",
"about.yes": "ja",
"about.no": "nein",
"about.section_results": "Ergebnisse",
"about.total_time": "Gesamtdauer",
"about.raw_chars": "Rohtranskript",
"about.processed_chars": "Verarbeitetes Transkript",
"about.summary_chars": "Zusammenfassung",
"about.timing_prompt": "Prompt",
"about.timing_duration": "Dauer",
```

- [ ] **Step 3: Add keys to `ru.js`**

Insert into `vts/static/i18n/ru.js` (after `"results.pending"`):

```javascript
"about.open": "О задаче",
"about.title": "О задаче",
"about.section_source": "Источник",
"about.created": "Создано",
"about.section_params": "Параметры запуска",
"about.language": "Язык",
"about.language_auto": "авто",
"about.audio_only": "Только аудио",
"about.transcript": "Транскрипт",
"about.prompts": "Промпты",
"about.yes": "да",
"about.no": "нет",
"about.section_results": "Результаты",
"about.total_time": "Общее время",
"about.raw_chars": "Сырой транскрипт",
"about.processed_chars": "Обработанный транскрипт",
"about.summary_chars": "Саммари",
"about.timing_prompt": "Промпт",
"about.timing_duration": "Длительность",
```

- [ ] **Step 4: Verify no JS errors and keys resolve**

Run: `cd tests/ui && node run.mjs`
Expected: `UI VERIFY: PASSED`. (i18n files are plain object literals; a trailing-comma/syntax slip would throw on boot and fail `smoke-boot`.)

- [ ] **Step 5: Commit**

```bash
git add vts/static/i18n/en.js vts/static/i18n/de.js vts/static/i18n/ru.js
git commit -m "i18n: About-task dialog keys (en/de/ru)"
```

---

## Task 6: UI scenario `task-about-dialog.mjs`

**Files:**
- Create: `tests/ui/scenarios/task-about-dialog.mjs`

**Interfaces:**
- Consumes harness helpers: `startStubServer`, `launch`, `openPage`, `isVisible`, `clickReal`, `dialogOpen` (from `../harness.mjs`, all already used by `restart-dialog.mjs`).

- [ ] **Step 1: Write the scenario**

Create `tests/ui/scenarios/task-about-dialog.mjs`:

```javascript
// Verifies the About-task dialog: hidden (and display:none) on boot — anti-flicker
// regression; opens from the stats chip; shows run parameters (language + prompt
// names); shows the results section with per-prompt finalize timings for a
// completed task; closes via the X button.
import { startStubServer, launch, openPage, isVisible, clickReal, dialogOpen } from "../harness.mjs";

export const name = "task-about-dialog";

const TASK = {
  id: "44444444-4444-4444-4444-444444444444",
  source_url: "http://x/v", source_title: "About me",
  status: "completed", summary_path: "/x/summary/final.md", transcript_path: "/x/t.txt",
  options: {
    language: "russian", audio_only: false, transcript: true,
    prompts: [{ source: "system", id: "summary" }, { source: "user", id: "u1" }],
    prompt_results: [
      { source: "system", id: "summary", name: "Summary", path: "/x/final.md", status: "completed" },
      { source: "user", id: "u1", name: "My memo", path: "/x/results/user__u1.md", status: "completed" },
    ],
  },
  steps: [
    { name: "summarize_final", status: "completed", started_at: "2026-06-28T10:01:00Z", finished_at: "2026-06-28T10:02:00Z" },
    { name: "finalize:user:u1", status: "completed", started_at: "2026-06-28T10:02:00Z", finished_at: "2026-06-28T10:03:30Z" },
  ],
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:03:30Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 2, total: 2 } },
  stats: { processing_seconds: 210, transcript_chars: 1000, redacted_chars: 800, summary_chars: 300,
           media_seconds: 600, media_bytes: 1048576 },
};

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // ANTI-FLICKER: closed dialog must be display:none right after boot.
    const closedDisplay = await page.evaluate(() => {
      const d = document.getElementById("task-about-dialog");
      return d ? getComputedStyle(d).display : "MISSING";
    });
    if (closedDisplay !== "none") {
      failures.push(`closed About-dialog display is ${JSON.stringify(closedDisplay)}, expected "none"`);
    }
    if (await isVisible(page, "#task-about-dialog")) {
      failures.push("closed About-dialog is visible on boot");
    }

    // The chip must be present and visible (task has media metrics).
    if (!(await isVisible(page, ".task-stats-chip"))) {
      failures.push(".task-stats-chip not visible (should show with media metrics)");
      return failures;
    }

    // Click the chip -> dialog opens.
    await clickReal(page, ".task-stats-chip");
    await page.waitForTimeout(200);
    if (!(await dialogOpen(page, "task-about-dialog"))) {
      failures.push("About-dialog did not open from the chip");
      return failures;
    }

    // Params section: language + both prompt names.
    const info = await page.evaluate(() => {
      const q = (s) => document.querySelector(s)?.textContent || "";
      return {
        language: q(".about-language"),
        prompts: q(".about-prompts"),
        resultsHidden: document.querySelector(".about-results-section")?.classList.contains("hidden"),
        timingRows: [...document.querySelectorAll(".about-prompt-timings tr")].map(
          (tr) => [...tr.children].map((td) => td.textContent)
        ),
      };
    });
    if (info.language !== "russian") failures.push(`language wrong: ${JSON.stringify(info.language)}`);
    if (!info.prompts.includes("My memo")) failures.push(`prompts missing user name: ${JSON.stringify(info.prompts)}`);
    if (info.resultsHidden) failures.push("results section hidden for a completed task");
    if (info.timingRows.length !== 2) {
      failures.push(`expected 2 timing rows, got ${info.timingRows.length}: ${JSON.stringify(info.timingRows)}`);
    } else {
      // user:u1 finalize ran 10:02:00 -> 10:03:30 = 1:30
      const userRow = info.timingRows.find((r) => r[0] === "My memo");
      if (!userRow) failures.push(`no timing row for "My memo": ${JSON.stringify(info.timingRows)}`);
      else if (userRow[1] !== "01:30") failures.push(`user timing wrong: ${JSON.stringify(userRow)}`);
    }

    // Close via X.
    await clickReal(page, "#task-about-close-btn");
    await page.waitForTimeout(150);
    if (await dialogOpen(page, "task-about-dialog")) failures.push("About-dialog did not close via X");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 2: Run the new scenario (expect PASS with the implementation from Tasks 1-5)**

Run: `cd tests/ui && node run.mjs`
Expected: `PASS  task-about-dialog` and `UI VERIFY: PASSED`.

- [ ] **Step 3: Sanity-check the test actually guards (temporary break)**

Temporarily edit `vts/static/styles.css` and change `#task-about-dialog[open]` to `#task-about-dialog` (remove the `[open]` gate). Run `cd tests/ui && node run.mjs`. Expected: `FAIL task-about-dialog` with the "closed About-dialog display ... expected none" message (proves the anti-flicker assertion works). Then revert the change and re-run to confirm PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/ui/scenarios/task-about-dialog.mjs
git commit -m "test(ui): About-task dialog scenario incl. anti-flicker + prompt timings"
```

---

## Task 7: Version bump + final verification

**Files:**
- Modify: `vts/__init__.py:3`

- [ ] **Step 1: Bump the version**

In `vts/__init__.py`, change line 3:

```python
__version__ = "1.1.18"
```

- [ ] **Step 2: Full UI verify**

Run: `cd tests/ui && node run.mjs`
Expected: all scenarios PASS, including `task-about-dialog`, `smoke-boot`, `restart-dialog`, `tooltip-icon-buttons`. `UI VERIFY: PASSED`.

- [ ] **Step 3: Commit**

```bash
git add vts/__init__.py
git commit -m "chore: bump version to 1.1.18 (About-task dialog)"
```

---

## Self-Review Notes

**Spec coverage:**
- Chip trigger (icon + visibility by media metric) → Tasks 1, 2.
- Dialog with Source / Params / Results sections → Tasks 3, 4.
- Per-prompt finalize-only duration → Task 4 (`aboutPromptTimings`, `finalizeStepName` + step start/finish).
- `.task-message` success removed, error retained → Task 4 (`resolveTaskMessage` returns only failure).
- Anti-flicker (no `open` attr, `[open]` gating, global `dialog:not([open])`, computed-display test) → Tasks 1, 3, 6.
- Stats mapping raw=`transcript_chars`, processed=`redacted_chars`, summary=`summary_chars` → Task 4 (`formatResultStats`) + dialog labels (Task 3/5).
- i18n en/de/ru → Task 5.
- UI test → Task 6.
- Version bump → Task 7.

**Edge cases from spec:** queued task without media metrics → chip stays hidden (Task 2 keeps existing `hidden` toggle). In-progress task → results section hidden (Task 4 `completed` gate). Prompt without finished finalize step → duration "—" (Task 4 `aboutPromptTimings`). Null stats → `formatMetricChars`/`formatMetricDuration` already return `stats.unknown`.

**Open implementation note for the engineer:** In Task 4 Step 3, confirm the `root._elements = { ... }` object literal's closing brace line number before inserting the chip click handler; the handler must come after that assignment so `root._elements.statsEl` is defined. Read the lines around 1588-1645 first.

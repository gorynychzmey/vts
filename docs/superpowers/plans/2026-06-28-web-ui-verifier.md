# Web UI Verifier (verifier-web) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A locally-run skill, `verifier-web`, that boots the real VTS static frontend against stubbed `/api/*`, drives it in a real Chromium (Playwright), and reports pass/fail with screenshots — run before tagging a build when the frontend changed.

**Architecture:** A reusable Node harness (`tests/ui/harness.mjs`) starts an http stub server that serves `vts/static/*` and answers `/api/*` from a default map merged with per-scenario overrides; it launches Chromium and exposes black-box assertion helpers (visibility, dialog-open, bounding box, real clicks, screenshots). Scenarios (a fixed smoke set + ad-hoc per-change scripts) are plain Node scripts using the harness. The `verifier-web` skill ensures Playwright/Chromium are present, runs scenarios, and reports. This lifts an already-working prototype into a clean, committed structure.

**Tech Stack:** Node 22, Playwright (chromium), plain ESM `.mjs`, Node's `http`/`fs`. No bundler. The VTS frontend itself stays dependency-free vanilla JS — Playwright lives only under `tests/ui/`.

**Spec:** [docs/superpowers/specs/2026-06-28-web-ui-verifier-design.md](../specs/2026-06-28-web-ui-verifier-design.md)

## Global Constraints

- The verifier serves the REAL static assets from `/home/victor/dev/vts/vts/static` and rewrites `__VTS_VERSION__` → a literal (e.g. `"verify"`).
- Default stub `/api/*`: `/api/version`→`{version}`, `/api/me`→`{requested_by,acting_as,is_admin:false}`, `/api/push/config`→`{enabled:false}`, `/api/tasks`→`[]`, `/api/prompts`→`[{source:"system",id:"summary",name:"Summary",editable:false},{source:"user",id:"u1",name:"Memo",editable:true}]`. Any key overridable per scenario; unknown `/api/*` → `{}` 200; write calls (POST/DELETE) → 200 stub so submit flows complete.
- Black-box first: real Playwright clicks on real selectors + observable-state assertions. White-box (`page.evaluate(() => internalFn(...))`) ONLY as a labeled shortcut.
- Scenarios MUST assert closed/not-yet-opened/disabled states, not only happy paths (the shipped bug lived in the closed state).
- Benign console noise to ignore: the EventSource MIME warning (`EventSource's response has a MIME type ("application/json")`).
- Chromium: Playwright uses the cached build. If `chromium.launch()` errors with "Executable doesn't exist", run `npx playwright install chromium` once.
- Playwright is a `tests/ui/`-local dev dependency (its own `package.json`); the VTS project root stays without a `package.json`. `tests/ui/node_modules/` must be gitignored.
- Skill lives at `.claude/skills/verifier-web/SKILL.md` (where the `verify` skill discovers `verifier-*`).

---

## File Structure

**New:**
- `tests/ui/package.json` — declares Playwright dev dep (isolates it from the vanilla-JS project root).
- `tests/ui/harness.mjs` — the reusable core: `startStubServer`, `launch`, `openPage`, assertion helpers, `screenshot`.
- `tests/ui/scenarios/restart-dialog.mjs` — smoke scenario for the restart dialog (the bug that motivated this).
- `tests/ui/scenarios/prompt-select.mjs` — smoke scenario for the create-form prompt dropdown + popover.
- `tests/ui/run.mjs` — runs all `scenarios/*.mjs`, aggregates pass/fail, prints a summary + screenshot paths, exits non-zero on any failure.
- `tests/ui/self-check.mjs` — proves the harness detects a known regression (negative test: inject bad CSS via override, assert FAIL).
- `.claude/skills/verifier-web/SKILL.md` — the skill.
- `.gitignore` — add `tests/ui/node_modules/`.

**Responsibilities:** `harness.mjs` knows nothing about specific scenarios; scenarios know nothing about each other; `run.mjs` orchestrates; the skill documents how/when to run.

---

## Task 1: Playwright dependency + harness core

**Files:**
- Create: `tests/ui/package.json`, `tests/ui/harness.mjs`
- Modify: `.gitignore`
- Test: `tests/ui/scenarios/smoke-boot.mjs` (a trivial scenario that exercises the harness end to end)

**Interfaces:**
- Produces (exported from `tests/ui/harness.mjs`):
  - `startStubServer(overrides = {}) -> Promise<{ server, baseUrl, port }>`
  - `launch() -> Promise<browser>` (Playwright chromium)
  - `openPage(browser, baseUrl) -> Promise<{ page, errors }>` (errors: string[] of pageerror+console.error, EventSource noise filtered)
  - `isVisible(page, selector) -> Promise<boolean>`
  - `dialogOpen(page, id) -> Promise<boolean>`
  - `computed(page, selector, prop) -> Promise<string>`
  - `boundingBox(page, selector) -> Promise<{x,y,width,height}|null>`
  - `clickReal(page, selector) -> Promise<void>`
  - `screenshot(page, name) -> Promise<string>` (returns the saved path)
  - `STATIC_DIR` constant = `/home/victor/dev/vts/vts/static`
  - `DEFAULT_API` constant (the default stub map)

- [ ] **Step 1: Create the isolated package + gitignore**

`tests/ui/package.json`:
```json
{
  "name": "vts-ui-verifier",
  "private": true,
  "type": "module",
  "devDependencies": { "playwright": "^1.40.0" }
}
```

Append to `.gitignore`:
```
tests/ui/node_modules/
```

- [ ] **Step 2: Install Playwright + browser**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && npm install
npx playwright install chromium
```
Expected: `playwright` installed under `tests/ui/node_modules`; chromium build present. (If `npm install` warns about peer deps, ignore.)

- [ ] **Step 3: Write the harness core**

`tests/ui/harness.mjs`:
```javascript
import { chromium } from "playwright";
import http from "http";
import fs from "fs";
import path from "path";

export const STATIC_DIR = "/home/victor/dev/vts/vts/static";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

export const DEFAULT_API = {
  "/api/version": { version: "verify" },
  "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
  "/api/push/config": { enabled: false },
  "/api/tasks": [],
  "/api/prompts": [
    { source: "system", id: "summary", name: "Summary", editable: false },
    { source: "user", id: "u1", name: "Memo", editable: true },
  ],
};

// overrides: { "/api/...": value }  (value = JSON-serializable). Also supports
// an optional `__extraCss` key: a CSS string injected before </head>, used by
// the self-check to simulate a regression.
export async function startStubServer(overrides = {}) {
  const extraCss = overrides.__extraCss || "";
  const api = { ...DEFAULT_API, ...overrides };
  delete api.__extraCss;
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      // Write calls: return a 200 stub so submit/POST flows complete.
      if (req.method !== "GET") { res.end(JSON.stringify({ status: "ok" })); return; }
      res.end(JSON.stringify(url in api ? api[url] : {}));
      return;
    }
    let f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC_DIR, f);
    if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    let body = fs.readFileSync(fp).toString();
    if (f === "/index.html") {
      body = body.replaceAll("__VTS_VERSION__", "verify");
      if (extraCss) body = body.replace("</head>", `<style id="verify-extra">${extraCss}</style></head>`);
    }
    res.setHeader("Content-Type", CT[path.extname(fp)] || "text/plain");
    res.end(body);
  });
  await new Promise((r) => server.listen(0, r));
  const port = server.address().port;
  return { server, baseUrl: `http://localhost:${port}`, port };
}

export async function launch() {
  return chromium.launch();
}

export async function openPage(browser, baseUrl) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("EventSource")) {
      errors.push("console.error: " + m.text());
    }
  });
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  return { page, errors };
}

export async function isVisible(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden" && el.offsetHeight > 0;
  }, selector);
}

export async function dialogOpen(page, id) {
  return page.evaluate((i) => {
    const d = document.getElementById(i);
    return !!d && d.open === true;
  }, id);
}

export async function computed(page, selector, prop) {
  return page.evaluate(([sel, p]) => {
    const el = document.querySelector(sel);
    return el ? getComputedStyle(el)[p] : null;
  }, [selector, prop]);
}

export async function boundingBox(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) };
  }, selector);
}

export async function clickReal(page, selector) {
  await page.click(selector);
}

const SHOT_DIR = "/tmp/vts-ui-verify";
export async function screenshot(page, name) {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const p = `${SHOT_DIR}/${name}.png`;
  await page.screenshot({ path: p });
  return p;
}
```

- [ ] **Step 4: Write a trivial harness smoke scenario to prove the core works**

`tests/ui/scenarios/smoke-boot.mjs`:
```javascript
// Boots the app, asserts the page loaded with no JS errors and the create form is present.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "smoke-boot";

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));
    if (!(await isVisible(page, "#task-form"))) failures.push("#task-form not visible after boot");
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 5: Run it**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && node -e "import('./scenarios/smoke-boot.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL '+JSON.stringify(f) : 'PASS'); process.exit(f.length?1:0); })"
```
Expected: `PASS`. (If it fails because `#task-form` has a different id, fix the selector to the real create-form element — check `vts/static/index.html` for the form's id; the form is `id="task-form"` per app.js `const form = document.getElementById("task-form")`.)

- [ ] **Step 6: Commit**

```bash
git add tests/ui/package.json tests/ui/harness.mjs tests/ui/scenarios/smoke-boot.mjs .gitignore
git commit -m "feat(verifier): UI harness core + boot smoke scenario"
```

---

## Task 2: Restart-dialog smoke scenario

**Files:**
- Create: `tests/ui/scenarios/restart-dialog.mjs`
- Test: the scenario IS the test; run it.

**Interfaces:**
- Consumes: `startStubServer`, `launch`, `openPage`, `isVisible`, `dialogOpen`, `clickReal`, `boundingBox`, `screenshot`.
- Produces: `export const name`, `export async function run() -> Promise<string[]>` (empty array = pass).

This scenario encodes the exact regression that motivated the verifier: the closed dialog must be hidden; the dialog must close via the × button. It renders a completed task (so the restart menu appears), opens the dialog through the real menu, and asserts closed-state + close behavior.

- [ ] **Step 1: Write the scenario**

`tests/ui/scenarios/restart-dialog.mjs`:
```javascript
// Verifies: closed restart dialog is hidden (not leaking onto the page);
// it opens from the task menu; it closes via the X button. This is the exact
// regression class that shipped (display:flex overrode closed <dialog> display:none).
import { startStubServer, launch, openPage, isVisible, dialogOpen, clickReal, screenshot } from "../harness.mjs";

export const name = "restart-dialog";

const COMPLETED_TASK = {
  id: "11111111-1111-1111-1111-111111111111",
  source_url: "http://x/v", source_title: "Test",
  status: "completed", summary_path: "/x/summary/final.md",
  options: {
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "summarize_windows", status: "completed", started_at: "2026-06-28T10:00:00Z", finished_at: "2026-06-28T10:01:00Z" },
    { name: "summarize_final", status: "completed", started_at: "2026-06-28T10:01:00Z", finished_at: "2026-06-28T10:02:00Z" },
  ],
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:02:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 2, total: 2 } }, stats: {},
};

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [COMPLETED_TASK] });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // CLOSED STATE (the critical assertion): the dialog must be hidden before any open.
    if (await isVisible(page, "#restart-final-dialog")) {
      failures.push("closed restart dialog is VISIBLE (should be display:none)");
    }

    // The restart menu button must exist on the rendered task row.
    if (!(await page.$(".restart-summary-btn"))) {
      failures.push("no .restart-summary-btn on the rendered task row");
      return failures; // can't proceed
    }

    // Open the menu, then click "Restart final summary only".
    await clickReal(page, ".restart-summary-btn");
    await page.waitForTimeout(150);
    const finalBtn = await page.$(".restart-summary-final-btn");
    if (!finalBtn) { failures.push("no .restart-summary-final-btn in menu"); return failures; }
    if (await finalBtn.isDisabled()) { failures.push(".restart-summary-final-btn is disabled (gate)"); return failures; }
    await finalBtn.click();
    await page.waitForTimeout(300);

    if (!(await dialogOpen(page, "restart-final-dialog"))) {
      failures.push("restart dialog did not open from the menu");
    } else {
      await screenshot(page, "restart-dialog-open");
      // CLOSE via the X button — must actually close.
      await clickReal(page, "#restart-final-close-btn");
      await page.waitForTimeout(200);
      if (await dialogOpen(page, "restart-final-dialog")) {
        failures.push("restart dialog did NOT close via the X button");
      }
      // And after closing, it must be hidden again.
      if (await isVisible(page, "#restart-final-dialog")) {
        failures.push("restart dialog visible after close");
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 2: Run it**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && node -e "import('./scenarios/restart-dialog.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL '+JSON.stringify(f) : 'PASS'); process.exit(f.length?1:0); })"
```
Expected: `PASS` (on current main, where the dialog fix is already deployed).

- [ ] **Step 3: Commit**

```bash
git add tests/ui/scenarios/restart-dialog.mjs
git commit -m "feat(verifier): restart-dialog smoke scenario (closed-hidden + closes)"
```

---

## Task 3: Prompt-select (create form) smoke scenario

**Files:**
- Create: `tests/ui/scenarios/prompt-select.mjs`
- Test: run it.

**Interfaces:**
- Consumes: harness exports. Produces: `name`, `run()`.

Covers the create-form prompt dropdown + popover — the OTHER thing that broke (popover always visible). Asserts: the popover is hidden until the toggle is clicked, opens on click, closes on outside click.

- [ ] **Step 1: Write the scenario**

`tests/ui/scenarios/prompt-select.mjs`:
```javascript
// Verifies the create-form prompt selector: the popover is hidden by default,
// opens when the toggle is clicked, and closes on an outside click. (The popover
// once shipped always-visible because a class rule overrode [hidden].)
import { startStubServer, launch, openPage, isVisible, clickReal } from "../harness.mjs";

export const name = "prompt-select";

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // The selector container renders.
    if (!(await page.$("#prompt-select .prompt-select-toggle"))) {
      failures.push("no prompt-select toggle in create form");
      return failures;
    }
    // CLOSED STATE: popover hidden before any interaction.
    if (await isVisible(page, "#prompt-select .prompt-select-popover")) {
      failures.push("prompt-select popover VISIBLE before opening (should be hidden)");
    }
    // Open via toggle.
    await clickReal(page, "#prompt-select .prompt-select-toggle");
    await page.waitForTimeout(150);
    if (!(await isVisible(page, "#prompt-select .prompt-select-popover"))) {
      failures.push("popover did not open on toggle click");
    }
    // Close via outside click (click the page header).
    await clickReal(page, "h1");
    await page.waitForTimeout(150);
    if (await isVisible(page, "#prompt-select .prompt-select-popover")) {
      failures.push("popover did not close on outside click");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
```

- [ ] **Step 2: Run it**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && node -e "import('./scenarios/prompt-select.mjs').then(async m => { const f = await m.run(); console.log(f.length ? 'FAIL '+JSON.stringify(f) : 'PASS'); process.exit(f.length?1:0); })"
```
Expected: `PASS`. (If the toggle selector differs, check `renderPromptMultiselect` in `vts/static/app.js` — it builds `.prompt-select-toggle` and `.prompt-select-popover` inside `#prompt-select`. If clicking `h1` doesn't register as "outside", use a different always-present element outside `#prompt-select`, e.g. `header p` or `#tasks` — verify against index.html.)

- [ ] **Step 3: Commit**

```bash
git add tests/ui/scenarios/prompt-select.mjs
git commit -m "feat(verifier): prompt-select popover smoke scenario"
```

---

## Task 4: Runner + harness self-check (negative test)

**Files:**
- Create: `tests/ui/run.mjs`, `tests/ui/self-check.mjs`
- Test: run both.

**Interfaces:**
- Consumes: every `scenarios/*.mjs` (each exports `name` + `run()`), harness exports.
- Produces: `run.mjs` is the entry point the skill calls; exits 0 if all scenarios pass, 1 otherwise. `self-check.mjs` proves the harness can FAIL (detects an injected regression).

- [ ] **Step 1: Write the runner**

`tests/ui/run.mjs`:
```javascript
// Runs every scenario in scenarios/, prints a summary, exits non-zero on any failure.
import fs from "fs";
import path from "path";
import url from "url";

const here = path.dirname(url.fileURLToPath(import.meta.url));
const scenDir = path.join(here, "scenarios");
const files = fs.readdirSync(scenDir).filter((f) => f.endsWith(".mjs")).sort();

let anyFail = false;
for (const file of files) {
  const mod = await import(path.join(scenDir, file));
  const label = mod.name || file;
  let failures;
  try {
    failures = await mod.run();
  } catch (e) {
    failures = ["threw: " + e.message];
  }
  if (failures.length) {
    anyFail = true;
    console.log(`FAIL  ${label}`);
    for (const f of failures) console.log(`        - ${f}`);
  } else {
    console.log(`PASS  ${label}`);
  }
}
console.log(anyFail ? "\nUI VERIFY: FAILED" : "\nUI VERIFY: PASSED");
process.exit(anyFail ? 1 : 0);
```

- [ ] **Step 2: Run the full suite**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && node run.mjs
```
Expected: `PASS` for smoke-boot, restart-dialog, prompt-select; final `UI VERIFY: PASSED`; exit 0.

- [ ] **Step 3: Write the self-check (proves the harness detects regressions)**

`tests/ui/self-check.mjs`:
```javascript
// Negative test: inject the exact bad CSS that shipped (display:flex on the
// closed restart dialog) via the stub server's __extraCss, and confirm the
// harness OBSERVES the dialog as visible-when-closed — i.e. the verifier can
// actually catch this class of regression, not just pass everything.
import { startStubServer, launch, openPage, isVisible } from "./harness.mjs";

const BAD_CSS = "#restart-final-dialog { display: flex !important; }";

const { server, baseUrl } = await startStubServer({ __extraCss: BAD_CSS });
const browser = await launch();
let detected = false;
try {
  const { page } = await openPage(browser, baseUrl);
  // With the bad CSS, the CLOSED dialog should be visible — the harness must see it.
  detected = await isVisible(page, "#restart-final-dialog");
} finally {
  await browser.close();
  server.close();
}
console.log(detected
  ? "SELF-CHECK PASSED: harness detects the closed-dialog-visible regression"
  : "SELF-CHECK FAILED: harness did NOT detect the injected regression");
process.exit(detected ? 0 : 1);
```

- [ ] **Step 4: Run the self-check**

Run:
```bash
cd /home/victor/dev/vts/tests/ui && node self-check.mjs
```
Expected: `SELF-CHECK PASSED: harness detects the closed-dialog-visible regression`; exit 0. (This proves the verifier FAILS when the bug is present — i.e. it's not a rubber stamp.)

- [ ] **Step 5: Commit**

```bash
git add tests/ui/run.mjs tests/ui/self-check.mjs
git commit -m "feat(verifier): scenario runner + harness self-check (regression detection)"
```

---

## Task 5: The verifier-web skill

**Files:**
- Create: `.claude/skills/verifier-web/SKILL.md`
- Test: manual — invoke the skill's documented commands and confirm they run.

**Interfaces:**
- Consumes: `tests/ui/run.mjs`, `tests/ui/self-check.mjs`.

- [ ] **Step 1: Write the skill**

`.claude/skills/verifier-web/SKILL.md`:
```markdown
---
name: verifier-web
description: "Browser UI verifier for the VTS web frontend. Use before tagging a build when vts/static/* changed, or when asked to verify a UI change in a real browser. Boots the real static frontend with stubbed /api/* and drives it in real Chromium (Playwright)."
---

# verifier-web — VTS browser UI verifier

Drives the **real** VTS frontend in a **real Chromium** against a stub server,
to catch client-side UI bugs (CSS/JS rendering, event wiring, dialog/popover
visibility) that `node --check` and code review miss. Run it before tagging a
build when `vts/static/*` changed.

## When to use

- Before `build-X.Y.Z` when the diff touches `vts/static/*`.
- When asked to verify a UI change in a real browser.

## Setup (one-time)

```bash
cd /home/victor/dev/vts/tests/ui && npm install
npx playwright install chromium   # if chromium.launch() errors "Executable doesn't exist"
```

## Run the smoke set

```bash
cd /home/victor/dev/vts/tests/ui && node run.mjs
```
- Exit 0 + `UI VERIFY: PASSED` → the covered surfaces are healthy.
- Exit 1 → read the `FAIL <scenario>` lines + the listed observations. Screenshots are saved under `/tmp/vts-ui-verify/`.

## Confirm the verifier itself works (optional)

```bash
cd /home/victor/dev/vts/tests/ui && node self-check.mjs
```
Expected: `SELF-CHECK PASSED` — proves the harness detects the closed-dialog-visible regression class (it's not a rubber stamp).

## Verifying a change not covered by the smoke set (ad-hoc scenario)

Write a short scenario in `tests/ui/scenarios/<name>.mjs` using `harness.mjs`.
Rules:
- **Black-box first:** drive with real clicks (`clickReal`) on real selectors;
  assert observable state (`isVisible`, `dialogOpen`, `boundingBox`).
- **Assert closed/disabled states, not only the happy path** — the bug that
  motivated this verifier lived entirely in the closed state.
- White-box (`page.evaluate(() => internalFn(...))`) ONLY as a labeled
  shortcut when reaching a state by real interaction is disproportionately
  hard — never to replace the observable-state assertion.
- Override `/api/*` per scenario via `startStubServer({ "/api/tasks": [...] })`
  to render the state you need (e.g. a completed task to show the restart menu).

Each scenario exports `name` (string) and `run()` returning a `string[]` of
failures (empty = pass). `run.mjs` auto-discovers `scenarios/*.mjs`.

## Harness API (tests/ui/harness.mjs)

- `startStubServer(overrides) -> {server, baseUrl, port}` — serves real
  `vts/static/*`, stubs `/api/*` (defaults + overrides; non-GET → 200 stub).
- `launch()`, `openPage(browser, baseUrl) -> {page, errors}` (errors filter the
  benign EventSource MIME warning).
- `isVisible(page, sel)`, `dialogOpen(page, id)`, `computed(page, sel, prop)`,
  `boundingBox(page, sel)`, `clickReal(page, sel)`, `screenshot(page, name)`.

## Not for

- End-to-end against a live backend (no Postgres/Redis/LLM here).
- CI / `build.sh` integration (no browser there).
```

- [ ] **Step 2: Verify the skill's commands run**

Run (the exact commands the skill documents):
```bash
cd /home/victor/dev/vts/tests/ui && node run.mjs && node self-check.mjs
```
Expected: `UI VERIFY: PASSED` (exit 0) then `SELF-CHECK PASSED` (exit 0).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/verifier-web/SKILL.md
git commit -m "feat(verifier): verifier-web skill"
```

---

## Self-Review Notes

**Spec coverage:**
- Stub server + real Chromium → Task 1 (harness). ✓
- Core harness (startStubServer, launch/openPage, assertion helpers, screenshot) → Task 1. ✓
- Smoke set (restart dialog, prompt-select popover) → Tasks 2, 3. ✓
- Ad-hoc scenarios → documented in the skill (Task 5) + the scenario shape from Tasks 2/3. ✓
- Black-box first + white-box-as-labeled-shortcut → enforced in scenarios + skill rules. ✓
- Critical rule (assert closed/disabled states) → restart-dialog + prompt-select both assert closed-first; stated in skill. ✓
- Default base + per-scenario override + write-stub → Task 1 `startStubServer`. ✓
- Run model (local skill, before build, not CI) → Task 5 skill doc; CI exclusion stated. ✓
- Testing the verifier itself (positive self-check + negative injected-CSS check) → Task 4 self-check + the restart-dialog positive scenario. ✓
- Pinned Chromium / install-if-missing → Global Constraints + Task 1 Step 2 + skill setup. ✓
- Playwright isolated to tests/ui, project root stays vanilla, node_modules gitignored → Task 1. ✓

**Placeholder scan:** none — every step has concrete code/commands. The two "if the selector differs, check X" notes (Task 1 Step 5, Task 3 Step 2) name the exact source symbol to confirm against; they are guidance for a real-selector check, not placeholders.

**Type consistency:** harness export names (`startStubServer`, `launch`, `openPage`, `isVisible`, `dialogOpen`, `computed`, `boundingBox`, `clickReal`, `screenshot`, `STATIC_DIR`, `DEFAULT_API`) are used identically across Tasks 1–5. Scenario contract (`export const name`, `export async function run() -> string[]`) is consistent across Tasks 2, 3, and the runner in Task 4.

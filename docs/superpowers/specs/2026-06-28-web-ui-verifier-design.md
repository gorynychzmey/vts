# verifier-web — Browser UI verifier for VTS

**Status:** Design approved, ready for implementation plan
**Date:** 2026-06-28

## Problem

VTS's frontend is vanilla JS/HTML/CSS with no JS test framework. Three times
in recent work, "green static checks" (`node --check`, code review) shipped
real UI bugs that only a running browser would catch:

- prompt-select popover always visible (a class rule overrode `[hidden]`),
- restart dialog leaking onto the page + not closing (an id rule
  `display:flex` overrode the closed-`<dialog>` `display:none`),
- (caught pre-merge) localized-name regressions.

`node --check` validates syntax, not rendered behavior. We need a verifier
that drives the **real UI in a real browser** before a build, so UI
regressions are caught before deploy rather than in production.

A working prototype already exists (Playwright + cached Chromium + a stub
server) and proved its value: it reproduced the closed-dialog-visible bug that
a headless jsdom harness and a white-box direct-call attempt both missed.

## Goal

A locally-run skill, `verifier-web`, that boots the real static frontend
against stubbed `/api/*`, drives it in a real Chromium, and reports pass/fail
with screenshots — run before tagging a build whenever the frontend changed.

## Approach

Stub server + real Chromium (Playwright). NOT the full backend. The verifier
serves the real `vts/static/*` assets and intercepts `/api/*` with canned
JSON. A real browser executes the real JS/CSS, so client-side bugs (popover
visibility, dialog leak, layout) are caught; Postgres/Redis/LLM are not
needed. (Full end-to-end against a live backend is explicitly out of scope —
a separate concern if ever needed.)

**Why stub, not full app:** the bugs we keep hitting are client-side
(CSS/JS rendering and event wiring). A stub gives a fast, deterministic
browser run with no DB/Redis/LLM/auth setup.

## Components

### 1. Core harness (reusable, stable)

A Node module (e.g. `tests/ui/harness.mjs`) providing:

- **`startStubServer(overrides)`** — an `http.Server` that serves
  `vts/static/*` (rewriting `__VTS_VERSION__`) and answers `/api/*` from a
  default map merged with per-scenario `overrides`. Defaults cover the
  bootstrap calls: `/api/version`, `/api/me`, `/api/push/config`,
  `/api/tasks` (→ `[]`), `/api/prompts` (→ a couple of prompts). Any key may
  be overridden; **write calls** (e.g. `POST /api/tasks/{id}/restart_summary`)
  are answered with a 200 stub so submit flows complete. Returns `{server,
  baseUrl, port}`.
- **`launch()` / `openPage(baseUrl)`** — start Chromium (Playwright), open a
  page, capture `pageerror` + `console.error` (filtering the benign
  EventSource MIME warning). Returns `{browser, page, errors}`.
- **Assertion helpers** operating on observable state:
  - `isVisible(page, selector)` → `display !== "none" && offsetHeight > 0`.
  - `dialogOpen(page, id)` → the `<dialog open>` attribute.
  - `boundingBox(page, selector)`, `computed(page, selector, prop)`.
  - `clickReal(page, selector)` — a real Playwright click (default driver).
- **`screenshot(page, name)`** — saves to the run's scratch dir; the path is
  returned so the report can reference it.

**Pinned Chromium:** Playwright uses the cached browser build
(`chromium_headless_shell-1228` present in `~/.cache/ms-playwright`). The
plan documents installing the matching build if Playwright is upgraded.

### 2. Scenarios

Two kinds, both built on the core harness:

- **Smoke set** (fixed, regression guard for the fragile bits that already
  broke): create-form prompt dropdown opens/closes; restart dialog opens from
  the task menu, is hidden when closed, closes via × and via submit; results
  tab dropdown populates. These run before every build that touches the
  frontend.
- **Ad-hoc scenarios** (per-change): a short script written for the current
  diff — e.g. "open the restart dialog, assert closed-state hidden, click ×,
  assert closed; check rows are left-aligned." Authored when a change isn't
  covered by the smoke set.

### 3. The `verifier-web` skill

Lives at `.claude/skills/verifier-web/SKILL.md` (where the `verify` skill
looks for `verifier-*`). The skill:

1. Ensures Playwright + the matching Chromium are available (install if
   missing — one-time).
2. Runs the smoke set + any ad-hoc scenario for the current change.
3. Captures screenshots of the relevant surfaces.
4. Reports pass/fail with the failing observations and screenshot paths.

## Verification method

**Black-box first:** drive the UI as a user — real Playwright clicks on real
selectors (`.restart-summary-btn`, `#restart-final-close-btn`), assert
observable state (visible/hidden, position, `<dialog open>`). This is the
default because it caught the real bug that a white-box direct-call attempt
missed (white-box opened the dialog via `showModal()` and never exercised the
broken closed state).

**White-box only as a labeled shortcut:** calling an internal function (e.g.
`page.evaluate(() => openRestartFinalDialog(fakeTask))`) is allowed ONLY when
reaching a state through real interaction is disproportionately expensive, and
must be marked as a white-box shortcut in the scenario. It never replaces the
black-box assertion of the final observable state.

**Critical rule (from the bug we shipped):** scenarios MUST assert the
**closed / not-yet-opened / disabled** states, not only the happy path. The
dialog bug lived entirely in the closed state; a happy-path-only check passes
while the bug ships.

## Stub data

Default base (bootstrap) + per-scenario override of any endpoint:

- Defaults: `/api/version`, `/api/me` (fake authed user), `/api/push/config`
  (disabled), `/api/tasks` (`[]`), `/api/prompts` (system summary + a user
  prompt).
- Override examples: put a completed task in `/api/tasks` so the restart menu
  renders; put long prompt names in `/api/prompts` to check dialog width;
  stub `POST .../restart_summary` → 200 so submit closes the dialog.

No live backend, no recorded fixtures — scenarios declare exactly the data
they need.

## Run model

`verifier-web` is a **local skill, run before tagging a build** when the diff
touches the frontend (`vts/static/*`). NOT wired into GitHub CI or `build.sh`:
CI has no browser and `build.sh` runs tests inside the image container where
Playwright doesn't fit without significant rework. Flow: frontend changed →
run `verifier-web` → green → tag `build-X.Y.Z`. (A future convenience could
hook it into the `/build` skill, but that's out of scope here.)

## Out of scope

- Full end-to-end against a live backend (Postgres/Redis/LLM).
- Wiring the verifier into GitHub CI or the container build.
- Visual-regression / pixel-diff baselines (screenshots are for human review,
  not automated diffing).
- A general cross-project verifier — this is VTS-specific (`verifier-web`).

## Testing the verifier itself

The harness is plumbing; its "test" is that it reproduces a known bug and
confirms a known-good state:

- A self-check scenario asserts the (now-fixed) restart dialog is hidden when
  closed and closes via × — i.e. the exact regression it was built to catch,
  now passing on current `main`.
- A negative self-check (optional): temporarily inject the bad CSS
  (`display:flex` on the closed dialog) via an override and confirm the
  verifier FAILS — proving it actually detects the regression, not just
  passes everything.

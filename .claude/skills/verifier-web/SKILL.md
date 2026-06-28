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

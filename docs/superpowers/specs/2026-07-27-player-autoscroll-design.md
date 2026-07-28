# Player transcript autoscroll — design

**Date:** 2026-07-27
**Issue:** vts follow-up to vts-u6w / vts-at8 (VOS-111 player)
**Status:** approved (design), pending implementation

## Goal

On the `/player` page, keep the currently-playing transcript sentence visible
by auto-scrolling the transcript container as playback advances. A checkbox
(default ON) gates the behaviour. As soon as the user scrolls the transcript
manually, the checkbox unchecks and autoscroll stops. Re-checking it brings the
current sentence back into view and resumes autoscroll.

## Context

The `/player` page is a self-contained HTML document rendered by
`_player_page_html` in `vts/api/main.py`. It has no access to `app.js`, so all
UI text and logic live inline in the page's `<style>` / `<script>`.

Relevant existing structure:
- `.transcript` — the scroll container: `max-height: 40vh; overflow-y: auto`.
- Sentences render as `.cue` spans (clickable, seek to their own start).
- A `timeupdate` handler already tracks the active sentence and toggles the
  `.active` class on the current `.cue` as playback advances. Autoscroll hooks
  into exactly this transition.

## UI

A checkbox row between the media element and the transcript list:

> ☑ Autoscroll

- Default: **checked** (ON).
- Label localized (en/ru/de) via the same inline-message mechanism already used
  for the media-unavailable text (a small embedded `{lang: text}` map picked by
  `navigator.language`).
- English: "Autoscroll". Russian: "Автопрокрутка". German: "Auto-Scrollen".

## Behaviour

All logic lives in the existing IIFE in `_player_page_html`, alongside
`wireCues` and the `timeupdate` handler.

### Autoscroll on active-sentence change
When the active cue changes (the same transition that moves the `.active`
class) AND the checkbox is checked:
```
activeCue.scrollIntoView({ block: "center", behavior: "smooth" });
```
Current sentence sits in the vertical centre of the container, with context
visible above and below.

### Distinguishing our scroll from the user's ("we are scrolling" flag)
A boolean guard set around programmatic scrolls:
- Before calling `scrollIntoView`, set `programmaticScroll = true`.
- A `scroll` listener on `.transcript`: if `programmaticScroll` is set, this is
  our own scroll — ignore it.
- Any `scroll` event that fires while `programmaticScroll` is NOT set is a user
  scroll → uncheck the checkbox (autoscroll off).

Because `behavior: "smooth"` emits several `scroll` events asynchronously over
the animation, the flag is cleared on a short **debounce** (~150 ms) after the
last programmatic scroll event, not on the first one. A one-shot flag would
clear too early and let a later frame of our own smooth scroll be misread as a
user scroll.

### Resume (re-checking the box)
On the checkbox `change` event, when it becomes checked: immediately
`scrollIntoView({block:"center", behavior:"smooth"})` the current active cue
(bring it back into view) and let autoscroll continue on subsequent
`timeupdate`s.

### Known trade-off
If the user scrolls *during* our smooth-scroll animation, there is a narrow
window where their gesture can be attributed to us and missed. Our scroll is
short and the debounce is small (~150 ms), so this is rare and self-correcting
(the next user scroll after the animation settles is caught). Accepted.

## Isolation

- Checkbox markup: part of the `_player_page_html` template, between
  `{media_block}` and `{transcript_html}`.
- CSS: a small `.autoscroll-toggle` rule in the page's inline `<style>`.
- JS: state (`programmaticScroll`, debounce timer) and handlers inside the
  existing IIFE. No change to `app.js` or the main SPA.
- The transcript rebuild path (`rebuildTranscript`, fired on
  `transcript_updated`) must preserve the checkbox and re-bind nothing extra —
  the checkbox lives outside `.transcript`, so a rebuild of the list does not
  touch it.

## Testing

- **pytest (structural invariant):** the rendered page contains the autoscroll
  checkbox (checked by default) and the scroll logic (`scrollIntoView`,
  `block: "center"`, the checkbox id, the user-scroll → uncheck wiring).
- **node --check:** the generated page `<script>` parses cleanly (both media-
  present and media-gone variants).
- No JS unit runner and `verifier-web` does not cover `/player` (a backend
  route, not static), so final behavioural confirmation is a manual check in
  the real player after deploy.

## Out of scope

- Persisting the checkbox state across page loads (no localStorage) — resets to
  ON each visit.
- Autoscroll on the main SPA transcript tab — this is the `/player` page only.

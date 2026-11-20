// vts-lzad: /player/{recording_id} for a recording whose task is gone.
//
// There is no media to play (the media belongs to the task runtime), but the
// recording still owns its transcript — and a search hit links here with ?t=.
// The page must render the transcript and mark the cited sentence instead of
// showing only "media unavailable", which is what a page without media used
// to mean. The same holds for an archived task's page (media archived away,
// transcript kept). A page rendered without transcript_only keeps the old
// message-only behaviour.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderPlayerPage } from "../player-page.mjs";

export const name = "player-transcript-only";

const BLOCKS = [{
  label: "Speaker 1",
  sentences: [[0, 0], [1, 12.5], [2, 30]].map(([i, s]) => ({ start: s, text: `sentence ${i}` })),
}];

export async function run() {
  const pages = {
    "/transcript-only": renderPlayerPage({
      title: "recording", media_tag: null, blocks: BLOCKS, transcript_only: true,
    }),
    // An archived task: the media is gone, the recording kept its transcript,
    // and the page is still the task's (live wiring on).
    "/archived-task": renderPlayerPage({
      title: "recording", media_tag: null, blocks: BLOCKS, transcript_only: true,
      task_id: "11111111-2222-3333-4444-555555555555", as_user: null,
    }),
    "/media-gone": renderPlayerPage({
      title: "recording", media_tag: null, blocks: BLOCKS, task_id: null, as_user: null,
    }),
  };
  const server = http.createServer((req, res) => {
    if (req.url.startsWith("/api/events")) {
      // Held open like the real stream, so EventSource does not loop.
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(": open\n\n");
      return;
    }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(pages[new URL(req.url, "http://x").pathname] || "");
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;

  const browser = await launch();
  const failures = [];

  async function open(url) {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}${url}`, { waitUntil: "load" });
    await page.waitForFunction(() => document.readyState === "complete");
    const state = await page.evaluate(() => ({
      cues: document.querySelectorAll(".cue").length,
      cited: document.querySelector(".cue.cited")?.getAttribute("data-cue") ?? null,
      media: !!document.querySelector("video, audio"),
    }));
    await page.close();
    return { ...state, errors };
  }

  try {
    const only = await open("/transcript-only?t=13");
    if (only.cues !== 3) failures.push(`transcript-only: ${only.cues} cues rendered, expected 3`);
    if (only.cited !== "1") failures.push(`transcript-only: cited=${only.cited}, expected 1`);
    if (only.media) failures.push("transcript-only: a media element is on the page");
    if (only.errors.length) failures.push(`transcript-only: JS errors ${JSON.stringify(only.errors)}`);

    const archived = await open("/archived-task?t=13");
    if (archived.cues !== 3) failures.push(`archived-task: ${archived.cues} cues rendered, expected 3`);
    if (archived.cited !== "1") failures.push(`archived-task: cited=${archived.cited}, expected 1`);
    if (archived.media) failures.push("archived-task: a media element is on the page");
    if (archived.errors.length) failures.push(`archived-task: JS errors ${JSON.stringify(archived.errors)}`);

    const gone = await open("/media-gone?t=13");
    if (gone.cues !== 0) failures.push(`media-gone: ${gone.cues} cues rendered, expected none`);
    if (gone.errors.length) failures.push(`media-gone: JS errors ${JSON.stringify(gone.errors)}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

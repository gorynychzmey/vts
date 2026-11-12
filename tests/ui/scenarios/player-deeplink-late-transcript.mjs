// vts-vtma.2: a deep link must still land when the transcript arrives later.
//
// A citation can be followed while the task is still processing: the page
// opens with no transcript, and it streams in on transcript_updated. The
// player used to apply the link once, at load, against an empty list — so the
// passage was never marked. (A hook for re-applying it existed, but nothing
// called it.) The shared core now remembers the cited moment and places it
// when the sentences arrive.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderPlayerPage } from "../player-page.mjs";

export const name = "player-deeplink-late-transcript";

const TASK_ID = "44444444-5555-6666-7777-888888888888";

function blocks() {
  return Array.from({ length: 30 }, (_, i) => ({
    label: `Speaker ${i % 2 + 1}`,
    sentences: [{ start: i * 2, text: `sentence ${i}` }],
  }));
}

export async function run() {
  const html = renderPlayerPage({
    title: "recording",
    media_tag: "<audio controls></audio>",
    blocks: [], // still processing: nothing to show yet
    task_id: TASK_ID,
    as_user: null,
  });
  const sse = new Set();
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(": open\n\n");
      sse.add(res);
      req.on("close", () => sse.delete(res));
      return;
    }
    if (url === `/api/tasks/${TASK_ID}/transcript-entries`) {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ blocks: blocks() }));
      return;
    }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(html);
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  const browser = await launch();
  const failures = [];
  try {
    for (const [label, url, expected] of [
      ["?t= followed before the transcript existed", "/?t=41", "sentence 20"],
      ["?cue= followed before the transcript existed", "/?cue=7", "sentence 7"],
    ]) {
      const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
      const errors = [];
      page.on("pageerror", (e) => errors.push(String(e)));
      await page.goto(base + url, { waitUntil: "load" });
      for (let i = 0; i < 40 && sse.size === 0; i++) await page.waitForTimeout(50);
      for (const res of sse) res.write(`event: transcript_updated\ndata: ${JSON.stringify({ task_id: TASK_ID })}\n\n`);
      await page.waitForFunction(() => document.querySelectorAll(".cue").length === 30, null, { timeout: 3000 })
        .catch(() => failures.push(`${label}: the transcript never arrived`));
      const cited = await page.waitForFunction(
        (want) => document.querySelector(".cue.cited")?.textContent === want, expected, { timeout: 2000 },
      ).then(() => expected, async () => page.evaluate(() => document.querySelector(".cue.cited")?.textContent ?? null));
      if (cited !== expected) failures.push(`${label}: cited ${JSON.stringify(cited)}, expected ${expected}`);
      const inView = await page.waitForFunction(() => {
        const box = document.querySelector(".transcript")?.getBoundingClientRect();
        const el = document.querySelector(".cue.cited")?.getBoundingClientRect();
        return !!box && !!el && el.top >= box.top - 1 && el.bottom <= box.bottom + 1;
      }, null, { timeout: 3000 }).then(() => true, () => false);
      if (!inView) failures.push(`${label}: the cited sentence was not scrolled into view`);
      if (errors.length) failures.push(`${label}: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

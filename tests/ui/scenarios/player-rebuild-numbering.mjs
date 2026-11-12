// vts-vtma.1: a live rebuild must give every sentence the number it had on
// first paint.
//
// The page used to have two renderers — Python for the first paint, JS for a
// rebuild after transcript_updated — and they disagreed on what to skip: the
// server dropped a sentence with empty text or an unusable start, the JS kept
// it. So the same transcript was numbered differently before and after a
// rebuild, and a deep link (?cue=N) addressed another sentence once the page
// had rebuilt. One renderer now; this keeps it that way.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderPlayerPage } from "../player-page.mjs";

export const name = "player-rebuild-numbering";

const TASK_ID = "22222222-3333-4444-5555-666666666666";

// Each block carries a sentence the renderer has to skip.
const BLOCKS = [
  { label: "A", sentences: [
    { start: 0, text: "alpha" },
    { start: 1, text: "   " },
    { start: 2, text: "bravo" },
  ] },
  { label: "B", sentences: [
    { start: null, text: "no start" },
    { start: 4, text: "charlie" },
  ] },
  { label: "C", sentences: [{ start: 6, text: "" }] },
  { label: "D", sentences: [{ start: 8, text: "delta" }] },
];

export async function run() {
  const html = renderPlayerPage({
    title: "recording",
    media_tag: "<audio controls></audio>",
    blocks: BLOCKS,
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
      // The same transcript, with a marker so the rebuild is observable.
      res.end(JSON.stringify({ blocks: [...BLOCKS, { label: "E", sentences: [{ start: 10, text: "echo" }] }] }));
      return;
    }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(html);
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  const browser = await launch();
  const failures = [];
  const numbering = (page) => page.evaluate(() =>
    Object.fromEntries(Array.from(document.querySelectorAll(".cue"))
      .map((c) => [c.textContent, c.getAttribute("data-cue")])));
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base + "/", { waitUntil: "load" });
    const first = await numbering(page);
    const expected = { alpha: "0", bravo: "1", charlie: "2", delta: "3" };
    if (JSON.stringify(first) !== JSON.stringify(expected)) {
      failures.push(`first paint: ${JSON.stringify(first)}, expected ${JSON.stringify(expected)}`);
    }

    for (let i = 0; i < 40 && sse.size === 0; i++) await page.waitForTimeout(50);
    for (const res of sse) res.write(`event: transcript_updated\ndata: ${JSON.stringify({ task_id: TASK_ID })}\n\n`);
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".cue")).some((c) => c.textContent === "echo"),
      null, { timeout: 3000 }).catch(() => failures.push("the page never rebuilt after transcript_updated"));
    const rebuilt = await numbering(page);
    const expectedRebuilt = { ...expected, echo: "4" };
    if (JSON.stringify(rebuilt) !== JSON.stringify(expectedRebuilt)) {
      failures.push(`after the rebuild: ${JSON.stringify(rebuilt)}, expected ${JSON.stringify(expectedRebuilt)}`);
    }
    if (errors.length) failures.push(`JS errors ${JSON.stringify(errors)}`);
    await page.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

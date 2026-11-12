// vts-5yyo / VOS-134: following a citation must land on the passage.
//
// The seeking, highlighting and autoscrolling already existed (VOS-111); what
// this covers is the ADDRESSING added on top — arriving at the page already
// positioned, and marking WHICH fragment was cited rather than which one
// happens to be playing.
//
// The player is a standalone page rather than part of the SPA, so this serves
// the real page, rendered by the function the /player route uses, instead of
// using the shared stub server. A check that only read the source text would
// prove the code exists, not that a link works.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderPlayerPage } from "../player-page.mjs";

export const name = "player-deeplink";

function pageHtml() {
  return renderPlayerPage({
    title: "recording",
    // No src: nothing loads, so only the addressing is exercised here. The
    // seek itself is covered by player-behaviour.
    media_tag: "<audio controls></audio>",
    blocks: [{ sentences: [[0, 0], [1, 12.5], [2, 30]].map(([i, s]) => ({ start: s, text: `sentence ${i}` })) }],
    task_id: null,
    as_user: null,
  });
}

export async function run() {
  const html = pageHtml();
  const server = http.createServer((_req, res) => {
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(html);
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const port = server.address().port;

  const browser = await launch();
  const failures = [];

  async function check(label, url, expectedCue) {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}${url}`, { waitUntil: "load" });
    await page.waitForTimeout(150);

    const cited = await page.evaluate(
      () => document.querySelector(".cue.cited")?.getAttribute("data-cue") ?? null);
    const outline = await page.evaluate(() => {
      const el = document.querySelector(".cue.cited");
      return el ? getComputedStyle(el).outlineWidth : null;
    });

    if (cited !== expectedCue) {
      failures.push(`${label}: cited=${cited}, expected ${expectedCue}`);
    }
    // A citation nobody can see is not a citation.
    if (expectedCue !== null && (!outline || outline === "0px")) {
      failures.push(`${label}: the cited cue has no visible outline (${outline})`);
    }
    if (errors.length) failures.push(`${label}: JS errors ${JSON.stringify(errors)}`);
    await page.close();
  }

  try {
    await check("?t= lands on the containing cue", "/?t=31", "2");
    await check("?t= earlier in the recording", "/?t=13", "1");
    // The fragment form never reaches the server, which is the better default
    // for a link naming a moment in someone's recording.
    await check("#t= fragment form works too", "/#t=31", "2");
    await check("?cue= addresses a sentence directly", "/?cue=1", "1");
    // Nothing asked for, nothing marked — a page opened normally must not
    // claim a citation.
    await check("no parameter cites nothing", "/", null);
    // A malformed time must leave the player alone rather than seeking to 0:00,
    // which would look like the link worked.
    await check("malformed ?t=abc cites nothing", "/?t=abc", null);
    // A timecode can drift when the transcript is re-rendered; the cue a
    // citation named is the passage it quoted.
    await check("?cue= wins over a conflicting ?t=", "/?cue=0&t=31", "0");
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

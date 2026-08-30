// vts-5yyo / VOS-134: following a citation must land on the passage.
//
// The seeking, highlighting and autoscrolling already existed (VOS-111); what
// this covers is the ADDRESSING added on top — arriving at the page already
// positioned, and marking WHICH fragment was cited rather than which one
// happens to be playing.
//
// The player is a standalone page rather than part of the SPA, so this builds
// the page from the real template assets instead of using the shared stub
// server. The assets are the real ones: a check that only read the source text
// would prove the code exists, not that a link works.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch } from "../harness.mjs";

export const name = "player-deeplink";

const TEMPLATES = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/api/_templates",
);

function pageHtml() {
  const css = fs.readFileSync(path.join(TEMPLATES, "player.css"), "utf8");
  // The live-rebuild script is spliced in by the server; not needed here.
  const js = fs.readFileSync(path.join(TEMPLATES, "player.js"), "utf8")
    .replace("/*__LIVE_SCRIPT__*/", "");
  const cues = [[0, 0], [1, 12.5], [2, 30]].map(([i, s]) =>
    `<span class="cue" data-start="${s}" data-cue="${i}" role="button" tabindex="0">sentence ${i}</span>`
  ).join(" ");
  return `<!doctype html><html><head><style>${css}</style></head><body>
<audio controls></audio>
<ol class="transcript"><li class="block"><p class="block-body">${cues}</p></li></ol>
<label><input type="checkbox" id="autoscroll-toggle" checked> auto</label>
<script>${js}</script></body></html>`;
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

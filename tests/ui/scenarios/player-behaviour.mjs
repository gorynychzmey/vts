// vts-vtma.1: pins what the /player page DOES, before its logic moves into a
// core shared with the MCP Apps view.
//
// The page is the real one: `_player_page_html()` renders it in Python, the
// same function the route calls, so this scenario does not depend on how the
// page is assembled (server-rendered cues today, client-rendered after the
// extraction). Only the data around it is stubbed: a silent WAV long enough to
// seek in, the transcript-entries endpoint, and an SSE stream this scenario
// pushes events into.
//
// Covered: click and keyboard seek, the playing-line highlight, autoscroll and
// its pause on a manual scroll, a deep link seeking without playing, the live
// rebuild from transcript_updated, the media-unavailable state, and the
// localised autoscroll label.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { launch } from "../harness.mjs";

export const name = "player-behaviour";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const TASK_ID = "11111111-2222-3333-4444-555555555555";
const DURATION = 90;

// 20 blocks x 2 sentences, one sentence every 2 s: cue N starts at 2N seconds.
// Several times taller than the transcript box (max-height 40vh), so a manual
// scroll from the bottom cannot reach the top.
function blocks(prefix = "sentence") {
  const out = [];
  for (let b = 0; b < 20; b++) {
    const sentences = [];
    for (let s = 0; s < 2; s++) {
      const n = b * 2 + s;
      sentences.push({ start: n * 2, end: n * 2 + 2, text: `${prefix} ${n}` });
    }
    out.push({ label: `Speaker ${b % 2 + 1}`, sentences });
  }
  return out;
}

function python() {
  if (process.env.VTS_PYTHON) return process.env.VTS_PYTHON;
  const venv = path.join(REPO, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : "python3";
}

// The real page, rendered by the function the /player route uses.
function renderPage() {
  const script = `
import json, sys
from vts.api.routers.artifacts import _player_page_html
args = json.load(sys.stdin)
sys.stdout.write(_player_page_html(**args))
`;
  const args = {
    title: "recording.wav",
    media_tag: '<audio controls src="/media.wav"></audio>',
    blocks: blocks(),
    task_id: TASK_ID,
    as_user: null,
  };
  const r = spawnSync(python(), ["-c", script], {
    cwd: REPO, input: JSON.stringify(args), encoding: "utf8",
  });
  if (r.status !== 0) throw new Error(`rendering the player page failed: ${r.stderr}`);
  return r.stdout;
}

// Silent 8 kHz mono 8-bit PCM: small, and every browser plays it.
function silentWav(seconds) {
  const rate = 8000, n = rate * seconds;
  const buf = Buffer.alloc(44 + n, 0x80);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n, 4); buf.write("WAVE", 8);
  buf.write("fmt ", 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20);
  buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate, 28);
  buf.writeUInt16LE(1, 32); buf.writeUInt16LE(8, 34);
  buf.write("data", 36); buf.writeUInt32LE(n, 40);
  return buf;
}

function startServer(html) {
  const wav = silentWav(DURATION);
  const sse = new Set();
  let entries = blocks();
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/media.wav") {
      // Range support: without it Chromium cannot seek in the file at all.
      const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
      res.setHeader("Content-Type", "audio/wav");
      res.setHeader("Accept-Ranges", "bytes");
      if (m) {
        const start = Number(m[1]);
        const end = m[2] ? Number(m[2]) : wav.length - 1;
        res.statusCode = 206;
        res.setHeader("Content-Range", `bytes ${start}-${end}/${wav.length}`);
        res.end(wav.subarray(start, end + 1));
      } else {
        res.end(wav);
      }
      return;
    }
    if (url === "/api/events") {
      // Held open, as the real stream is: answering and closing would make
      // EventSource reconnect in a loop and hide whether events are handled.
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(": open\n\n");
      sse.add(res);
      req.on("close", () => sse.delete(res));
      return;
    }
    if (url === `/api/tasks/${TASK_ID}/transcript-entries`) {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ blocks: entries }));
      return;
    }
    if (url === "/") {
      res.setHeader("Content-Type", "text/html; charset=utf-8");
      res.end(html);
      return;
    }
    res.statusCode = 404;
    res.end("nf");
  });
  return {
    server,
    clients: () => sse.size,
    setEntries(b) { entries = b; },
    push(event, data) {
      for (const res of sse) res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    },
  };
}

export async function run() {
  const html = renderPage();
  const stub = startServer(html);
  await new Promise((resolve) => stub.server.listen(0, resolve));
  const base = `http://127.0.0.1:${stub.server.address().port}`;

  const browser = await launch();
  const failures = [];
  const fail = (msg) => failures.push(msg);

  async function open(url = "/", { locale } = {}) {
    const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    if (locale) {
      await page.addInitScript((l) => localStorage.setItem("vts_locale", l), locale);
    }
    await page.goto(base + url, { waitUntil: "load" });
    await page.waitForFunction(() => {
      const m = document.querySelector("audio");
      return m && m.readyState >= 1;
    }, null, { timeout: 5000 });
    return { page, errors };
  }

  const currentTime = (page) => page.evaluate(() => document.querySelector("audio").currentTime);
  const activeCue = (page) => page.evaluate(() => {
    const all = document.querySelectorAll(".cue.active");
    return all.length === 1 ? all[0].getAttribute("data-cue") : `${all.length} active cues`;
  });
  // Pause only once play() has resolved: pausing a pending play() rejects its
  // promise, which the page leaves unhandled — a test artefact, not a defect
  // in what the user sees.
  async function stopPlayback(page) {
    await page.evaluate(() => new Promise((resolve) => {
      const m = document.querySelector("audio");
      const done = () => { m.pause(); resolve(); };
      if (m.paused) { resolve(); return; }
      m.addEventListener("playing", done, { once: true });
      setTimeout(done, 2000);
    }));
  }
  async function seekTo(page, seconds) {
    await page.evaluate((s) => { document.querySelector("audio").currentTime = s; }, seconds);
  }
  async function waitActive(page, cue) {
    try {
      await page.waitForFunction((c) => {
        const all = document.querySelectorAll(".cue.active");
        return all.length === 1 && all[0].getAttribute("data-cue") === c;
      }, cue, { timeout: 3000 });
    } catch { /* reported by the caller's exact check */ }
  }
  // The cue lies fully inside the transcript box's visible area.
  const cueInView = (page, cue) => page.evaluate((c) => {
    const box = document.querySelector(".transcript").getBoundingClientRect();
    const el = document.querySelector(`.cue[data-cue="${c}"]`).getBoundingClientRect();
    return el.top >= box.top - 1 && el.bottom <= box.bottom + 1;
  }, cue);
  async function waitScrollSettled(page) {
    let last = -1;
    for (let i = 0; i < 40; i++) {
      const top = await page.evaluate(() => document.querySelector(".transcript").scrollTop);
      if (top === last) return top;
      last = top;
      await page.waitForTimeout(50);
    }
    return last;
  }

  try {
    // --- Click seeks and plays ---
    {
      const { page, errors } = await open();
      await page.click('.cue[data-cue="10"]');
      await page.waitForFunction(() => !document.querySelector("audio").paused, null, { timeout: 3000 })
        .catch(() => {});
      const t = await currentTime(page);
      const paused = await page.evaluate(() => document.querySelector("audio").paused);
      if (Math.abs(t - 20) > 0.5) fail(`click on cue 10 (starts 20 s): currentTime=${t}`);
      if (paused) fail("click on a cue did not start playback");
      await stopPlayback(page);

      // --- Keyboard: Enter and Space seek too ---
      await page.focus('.cue[data-cue="5"]');
      await page.keyboard.press("Enter");
      let k = await currentTime(page);
      if (Math.abs(k - 10) > 0.5) fail(`Enter on cue 5 (starts 10 s): currentTime=${k}`);
      await stopPlayback(page);
      await page.focus('.cue[data-cue="7"]');
      await page.keyboard.press(" ");
      k = await currentTime(page);
      if (Math.abs(k - 14) > 0.5) fail(`Space on cue 7 (starts 14 s): currentTime=${k}`);
      await stopPlayback(page);
      if (errors.length) fail(`click/keyboard: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Highlight follows playback position; exactly one active cue ---
    {
      const { page, errors } = await open();
      await page.evaluate(() => { document.getElementById("autoscroll-toggle").checked = false; });
      await seekTo(page, 31);
      await waitActive(page, "15");
      let a = await activeCue(page);
      if (a !== "15") fail(`at 31 s the active cue should be 15 (starts 30 s), got ${a}`);
      await seekTo(page, 7);
      await waitActive(page, "3");
      a = await activeCue(page);
      if (a !== "3") fail(`seeking back to 7 s should move the highlight to cue 3, got ${a}`);
      const bg = await page.evaluate(() => getComputedStyle(document.querySelector(".cue.active")).backgroundColor);
      if (!bg || bg === "rgba(0, 0, 0, 0)") fail(`the active cue has no visible highlight (${bg})`);
      if (errors.length) fail(`highlight: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Autoscroll: follows, pauses on a manual scroll, resumes on re-check ---
    {
      const { page, errors } = await open();
      const checked = () => page.evaluate(() => document.getElementById("autoscroll-toggle").checked);
      if (!(await checked())) fail("autoscroll should start enabled");

      await seekTo(page, 78);
      await waitActive(page, "39");
      await waitScrollSettled(page);
      if (!(await cueInView(page, "39"))) fail("with autoscroll on, the playing cue 39 was not scrolled into view");

      // A real wheel gesture over the box, after our own smooth scroll settled.
      await page.waitForTimeout(200);
      const box = await page.locator(".transcript").boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, -200);
      await page.waitForFunction(() => !document.getElementById("autoscroll-toggle").checked, null, { timeout: 2000 })
        .catch(() => {});
      if (await checked()) fail("a manual scroll of the transcript should switch autoscroll off");

      const before = await waitScrollSettled(page);
      await seekTo(page, 0);
      await waitActive(page, "0");
      const after = await waitScrollSettled(page);
      if (after !== before) fail(`with autoscroll off, playback moved the transcript (${before} -> ${after})`);
      if (await cueInView(page, "0")) fail("with autoscroll off, cue 0 should still be out of view");

      await page.click("#autoscroll-toggle");
      await waitScrollSettled(page);
      if (!(await cueInView(page, "0"))) fail("re-enabling autoscroll should bring the playing cue 0 back into view");
      if (errors.length) fail(`autoscroll: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Deep link on the real page: seeks there, marks it, does not play ---
    {
      const { page, errors } = await open("/?t=31");
      await page.waitForFunction(() => Math.abs(document.querySelector("audio").currentTime - 31) < 0.5, null,
        { timeout: 3000 }).catch(() => {});
      const t = await currentTime(page);
      const cited = await page.evaluate(() => document.querySelector(".cue.cited")?.getAttribute("data-cue") ?? null);
      const paused = await page.evaluate(() => document.querySelector("audio").paused);
      if (Math.abs(t - 31) > 0.5) fail(`?t=31 should seek the media to 31 s, currentTime=${t}`);
      if (cited !== "15") fail(`?t=31 should cite cue 15, got ${cited}`);
      if (!paused) fail("a deep link must not start playback on its own");
      if (errors.length) fail(`deep link: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Live rebuild from transcript_updated ---
    {
      const { page, errors } = await open();
      await page.waitForFunction(() => true);
      for (let i = 0; i < 40 && stub.clients() === 0; i++) await page.waitForTimeout(50);

      // Another task's event must leave this page alone.
      stub.setEntries(blocks("rebuilt"));
      stub.push("transcript_updated", { task_id: "99999999-0000-0000-0000-000000000000" });
      await page.waitForTimeout(300);
      const untouched = await page.evaluate(() => document.querySelector('.cue[data-cue="0"]').textContent);
      if (untouched !== "sentence 0") fail(`another task's transcript_updated rebuilt this page (${untouched})`);

      stub.push("transcript_updated", { task_id: TASK_ID });
      await page.waitForFunction(() => document.querySelector('.cue[data-cue="0"]')?.textContent === "rebuilt 0",
        null, { timeout: 3000 }).catch(() => {});
      const rebuilt = await page.evaluate(() =>
        Array.from(document.querySelectorAll(".cue")).map((c) => [c.getAttribute("data-cue"), c.textContent]));
      const expected = Array.from({ length: 40 }, (_, n) => [String(n), `rebuilt ${n}`]);
      if (JSON.stringify(rebuilt) !== JSON.stringify(expected)) {
        fail(`after transcript_updated the cues should be numbered 0..39 across blocks: ${JSON.stringify(rebuilt.slice(0, 6))}…`);
      }
      // A second rebuild (first assembly, then a re-render after speakers are
      // resolved) must number from 0 again, not continue where the last one
      // stopped — a deep link addresses the same sentence either way.
      stub.setEntries(blocks("again"));
      stub.push("transcript_updated", { task_id: TASK_ID });
      await page.waitForFunction(() => document.querySelector(".cue")?.textContent === "again 0",
        null, { timeout: 3000 }).catch(() => {});
      const again = await page.evaluate(() =>
        Array.from(document.querySelectorAll(".cue")).map((c) => c.getAttribute("data-cue")));
      if (JSON.stringify(again) !== JSON.stringify(expected.map(([n]) => n))) {
        fail(`a second rebuild should number the cues 0..39 again: ${JSON.stringify(again.slice(0, 4))}…`);
      }

      // Rebuilt cues are wired: click still seeks, the highlight still follows.
      // Found by text, not by data-cue: a numbering defect is reported above
      // and must not turn this check into a 30 s wait for a missing selector.
      await page.locator(".cue", { hasText: /^again 20$/ }).click({ timeout: 3000 });
      const t = await currentTime(page);
      await stopPlayback(page);
      if (Math.abs(t - 40) > 0.5) fail(`a rebuilt cue 20 (starts 40 s) did not seek: currentTime=${t}`);
      await page.waitForFunction(() => document.querySelector(".cue.active")?.textContent === "again 20",
        null, { timeout: 3000 }).catch(() => {});
      const a = await page.evaluate(() => document.querySelector(".cue.active")?.textContent ?? null);
      if (a !== "again 20") fail(`after a rebuild the highlight should follow playback to "again 20", got ${a}`);
      if (errors.length) fail(`live rebuild: JS errors ${JSON.stringify(errors)}`);
      stub.setEntries(blocks());
      await page.close();
    }

    // --- Media unavailable: deleted task, or media that fails to load ---
    {
      const { page, errors } = await open();
      for (let i = 0; i < 40 && stub.clients() === 0; i++) await page.waitForTimeout(50);
      stub.push("task_status", { task_id: TASK_ID, data: { status: "deleted" } });
      await page.waitForSelector("[data-media-unavailable]", { timeout: 3000 }).catch(() => {});
      const state = await page.evaluate(() => ({
        msg: document.querySelector("[data-media-unavailable]")?.textContent ?? null,
        media: !!document.querySelector("audio, video"),
        transcript: !!document.querySelector(".transcript"),
      }));
      if (state.msg !== "This media is no longer available.") fail(`deleted task: unavailable message ${JSON.stringify(state.msg)}`);
      if (state.media || state.transcript) fail(`deleted task: player or transcript still shown ${JSON.stringify(state)}`);
      if (errors.length) fail(`deleted task: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }
    {
      const { page, errors } = await open();
      await page.evaluate(() => { document.querySelector("audio").src = "/missing.wav"; });
      await page.waitForSelector("[data-media-unavailable]", { timeout: 3000 }).catch(() => {});
      const shown = await page.evaluate(() => !!document.querySelector("[data-media-unavailable]")
        && !document.querySelector("audio, video"));
      if (!shown) fail("media that fails to load should switch the page to the unavailable state");
      if (errors.length) fail(`media error: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- The autoscroll label follows the language chosen in the app ---
    {
      const { page, errors } = await open("/", { locale: "ru" });
      const label = await page.evaluate(() => document.querySelector("[data-autoscroll-label]").textContent);
      if (label !== "Автопрокрутка") fail(`with vts_locale=ru the autoscroll label is ${JSON.stringify(label)}`);
      if (errors.length) fail(`locale: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }
  } finally {
    await browser.close();
    stub.server.close();
  }
  return failures;
}

// vts-vtma.3: media in the MCP Apps player view.
//
// The page plays the HOST, as in mcp-player-view, and also serves the media:
// app_media_url answers with links into this server, some of which are made
// to fail. Covered: the player appears and drives the transcript (click seeks,
// highlight follows, a cited moment seeks without playing); a link that stops
// working is renewed once and keeps the position; a renewed link that fails
// too removes the player, keeps the transcript, and does not loop; no link
// means a transcript without a player.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderMcpPlayerView } from "../player-page.mjs";

export const name = "mcp-player-media";

const REC = "55555555-6666-7777-8888-999999999999";
const DURATION = 90;

function blocks() {
  return Array.from({ length: 40 }, (_, i) => ({
    label: `Голос ${i % 2 + 1}`,
    sentences: [{ start: i * 2, text: `sentence ${i}` }],
  }));
}

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

// The host. config.links: what successive app_media_url calls return (the
// last one repeats); a null entry means "no media".
const HOST = `<!doctype html><html><body>
<script>
window.__log = [];
window.__start = function(config) {
  var frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.style.width = "700px"; frame.style.height = "650px";
  frame.src = "/view";
  document.body.appendChild(frame);
  var mediaCalls = 0;
  function reply(msg) { frame.contentWindow.postMessage(msg, "*"); }
  window.addEventListener("message", function(event) {
    if (event.source !== frame.contentWindow) return;
    var msg = event.data;
    window.__log.push({ method: msg.method, params: msg.params });
    if (msg.method === "ui/initialize") {
      reply({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: "2026-01-26",
        hostCapabilities: {}, hostContext: config.hostContext || {} } });
    } else if (msg.method === "ui/notifications/initialized") {
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: {} } });
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: config.result });
    } else if (msg.method === "tools/call" && msg.params.name === "app_media_url") {
      var link = config.links[Math.min(mediaCalls, config.links.length - 1)];
      mediaCalls++;
      var data = link ? { url: location.origin + link, kind: "audio", expires_at: 0, reason: null }
                      : { url: null, kind: null, expires_at: null, reason: "no media" };
      reply({ jsonrpc: "2.0", id: msg.id, result: { content: [{ type: "text", text: "m" }], structuredContent: data } });
    } else if (msg.id !== undefined) {
      reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: "unexpected" }] } });
    }
  });
  window.__frame = frame;
};
</script></body></html>`;

export async function run() {
  const view = renderMcpPlayerView();
  const wav = silentWav(DURATION);
  const server = http.createServer((req, res) => {
    const url = req.url.split("?")[0];
    if (url === "/view") { res.setHeader("Content-Type", "text/html; charset=utf-8"); res.end(view); return; }
    if (url === "/ok.wav") {
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
    if (url.startsWith("/expired")) { res.statusCode = 404; res.end("gone"); return; }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(HOST);
  });
  await new Promise((resolve) => server.listen(0, resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  const browser = await launch();
  const failures = [];
  const fail = (m) => failures.push(m);

  async function start(config) {
    const page = await browser.newPage({ viewport: { width: 900, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base + "/", { waitUntil: "load" });
    await page.evaluate((c) => window.__start(c), config);
    const frame = await (await page.waitForSelector("iframe")).contentFrame();
    await frame.waitForFunction(() => document.querySelectorAll(".cue").length === 40, null, { timeout: 5000 })
      .catch(() => fail(`${config.label}: the transcript never rendered`));
    return { page, frame, errors };
  }
  const result = (atSec) => ({ content: [{ type: "text", text: "s" }], structuredContent: {
    recording_id: REC, title: "Call", duration_sec: DURATION, people: [], at_sec: atSec ?? null,
    blocks: blocks(), next_cursor: null } });
  const mediaCalls = (page) => page.evaluate(() =>
    window.__log.filter((m) => m.method === "tools/call" && m.params.name === "app_media_url").length);
  const waitReady = (frame) => frame.waitForFunction(() => {
    const m = document.querySelector("audio");
    return m && m.readyState >= 1;
  }, null, { timeout: 5000 }).then(() => true, () => false);

  try {
    // --- A working link: the player drives the transcript ---
    {
      const { page, frame, errors } = await start({ label: "working", result: result(31), links: ["/ok.wav"],
        hostContext: { locale: "ru" } });
      if (!(await waitReady(frame))) fail("working: the audio never loaded");
      // The cited moment seeks the media there, without playing.
      await frame.waitForFunction(() => Math.abs(document.querySelector("audio").currentTime - 31) < 0.5, null,
        { timeout: 3000 }).catch(() => {});
      const at = await frame.evaluate(() => ({ t: document.querySelector("audio").currentTime,
        paused: document.querySelector("audio").paused,
        cited: document.querySelector(".cue.cited")?.textContent ?? null }));
      if (Math.abs(at.t - 31) > 0.5) fail(`at_sec=31 should seek the media to 31 s, got ${at.t}`);
      if (!at.paused) fail("a cited moment must not start playback");
      if (at.cited !== "sentence 15") fail(`at_sec=31 should cite sentence 15, got ${at.cited}`);

      const label = await frame.evaluate(() => document.querySelector(".autoscroll-toggle span")?.textContent ?? null);
      if (label !== "Автопрокрутка") fail(`autoscroll toggle in the host's language: ${JSON.stringify(label)}`);

      await frame.click('.cue[data-cue="10"]');
      await frame.waitForFunction(() => Math.abs(document.querySelector("audio").currentTime - 20) < 0.6, null,
        { timeout: 3000 }).catch(() => {});
      const t = await frame.evaluate(() => document.querySelector("audio").currentTime);
      if (Math.abs(t - 20) > 0.6) fail(`a click on sentence 10 should seek to 20 s, got ${t}`);
      await frame.waitForFunction(() => document.querySelector(".cue.active")?.textContent === "sentence 10", null,
        { timeout: 3000 }).catch(() => {});
      const active = await frame.evaluate(() => document.querySelector(".cue.active")?.textContent ?? null);
      if (active !== "sentence 10") fail(`the highlight should follow playback to sentence 10, got ${active}`);
      await frame.evaluate(() => new Promise((r) => {
        const m = document.querySelector("audio");
        if (m.paused) return r();
        m.addEventListener("playing", () => { m.pause(); r(); }, { once: true });
        setTimeout(() => { m.pause(); r(); }, 2000);
      }));
      // The toggle is wired to the core: a manual scroll switches autoscroll off.
      await page.waitForTimeout(300);
      const fbox = await (await page.$("iframe")).boundingBox();
      const tbox = await frame.evaluate(() => {
        const r = document.querySelector(".transcript").getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      });
      await page.mouse.move(fbox.x + tbox.x, fbox.y + tbox.y);
      await page.mouse.wheel(0, 300);
      await frame.waitForFunction(() => !document.querySelector("[data-autoscroll]").checked, null, { timeout: 2000 })
        .catch(() => {});
      if (await frame.evaluate(() => document.querySelector("[data-autoscroll]").checked)) {
        fail("a manual scroll of the transcript should switch the autoscroll toggle off");
      }
      if ((await mediaCalls(page)) !== 1) fail(`a working link needs one app_media_url call, made ${await mediaCalls(page)}`);
      if (errors.length) fail(`working: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A link that stopped working is renewed, and playback goes on ---
    {
      const { page, frame, errors } = await start({ label: "renewed", result: result(), links: ["/expired.wav", "/ok.wav"] });
      if (!(await waitReady(frame))) fail("renewed: the renewed link never loaded");
      const src = await frame.evaluate(() => document.querySelector("audio")?.src ?? null);
      if (!src || !src.endsWith("/ok.wav")) fail(`renewed: the player should be on the fresh link, src=${src}`);
      if ((await mediaCalls(page)) !== 2) fail(`renewed: expected 2 app_media_url calls, made ${await mediaCalls(page)}`);
      if (errors.length) fail(`renewed: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A renewed link that fails too: player gone, transcript stays, no loop ---
    {
      const { page, frame, errors } = await start({ label: "gone", result: result(), links: ["/expired.wav"],
        hostContext: { locale: "en" } });
      await frame.waitForSelector("[data-media-gone]", { timeout: 5000 }).catch(() => {});
      await page.waitForTimeout(500);
      const state = await frame.evaluate(() => ({
        audio: !!document.querySelector("audio"),
        toggle: !!document.querySelector(".autoscroll-toggle"),
        note: document.querySelector("[data-media-gone]")?.textContent ?? null,
        cues: document.querySelectorAll(".cue").length,
      }));
      if (state.audio || state.toggle) fail(`gone: the player should be removed ${JSON.stringify(state)}`);
      if (state.note !== "The media cannot be played here; the transcript is still available.") {
        fail(`gone: note ${JSON.stringify(state.note)}`);
      }
      if (state.cues !== 40) fail(`gone: the transcript should stay, ${state.cues} cues`);
      const calls = await mediaCalls(page);
      if (calls !== 2) fail(`gone: a failing renewal must not loop — ${calls} app_media_url calls`);
      if (errors.length) fail(`gone: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- No link at all: a transcript without a player ---
    {
      const { page, frame, errors } = await start({ label: "none", result: result(), links: [null] });
      const state = await frame.evaluate(() => ({
        audio: !!document.querySelector("audio, video"),
        toggle: !!document.querySelector(".autoscroll-toggle"),
        gone: !!document.querySelector("[data-media-gone]"),
      }));
      if (state.audio || state.toggle || state.gone) fail(`none: expected a bare transcript ${JSON.stringify(state)}`);
      if (errors.length) fail(`none: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

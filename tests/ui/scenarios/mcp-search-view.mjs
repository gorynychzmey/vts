// vts-vtma.4: the MCP Apps search view against a host.
//
// The page plays the HOST: it loads the real `ui://vts/search` document into
// a sandboxed iframe, sends a search_transcripts result, and answers the
// tools the view calls — search_transcripts for the next page, show_recording
// when a passage is opened, app_player_blocks / app_media_url for the player.
// Covered: the list, "show more" driven by `total`, opening a passage in the
// player on its moment, going back, a slow answer for an abandoned passage not
// replacing the one opened after it, text shown as text, and the empty and
// error results.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderMcpSearchView } from "../player-page.mjs";

export const name = "mcp-search-view";

const A = "aaaaaaaa-0000-0000-0000-000000000001";
const B = "bbbbbbbb-0000-0000-0000-000000000002";
const C = "cccccccc-0000-0000-0000-000000000003";

function hit(recording, title, start, text, speakers = []) {
  return { recording_id: recording, source_task_id: null, title, text, start_sec: start, end_sec: start + 5,
    speakers, score: 0.7, transcript_url: null, player_url: null };
}

function recordingData(recording, title, atSec) {
  return {
    recording_id: recording, title, duration_sec: 120, people: [], at_sec: atSec,
    blocks: Array.from({ length: 30 }, (_, i) => ({ label: "", sentences: [{ start: i * 4, text: `${title} line ${i}` }] })),
    next_cursor: null,
  };
}

const HOST = `<!doctype html><html><body>
<script>
window.__log = [];
window.__start = function(config) {
  var frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.style.width = "700px"; frame.style.height = "650px";
  frame.src = "/view";
  document.body.appendChild(frame);
  function reply(msg) { frame.contentWindow.postMessage(msg, "*"); }
  function ok(id, data) { reply({ jsonrpc: "2.0", id: id, result: { content: [{ type: "text", text: "r" }], structuredContent: data } }); }
  window.addEventListener("message", function(event) {
    if (event.source !== frame.contentWindow) return;
    var msg = event.data;
    window.__log.push({ method: msg.method, params: msg.params });
    if (msg.method === "ui/initialize") {
      reply({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: "2026-01-26",
        hostCapabilities: {}, hostContext: config.hostContext || {} } });
    } else if (msg.method === "ui/notifications/initialized") {
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: config.args } });
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: config.result });
    } else if (msg.method === "tools/call") {
      var name = msg.params.name, a = msg.params.arguments;
      if (name === "search_transcripts") ok(msg.id, config.more);
      else if (name === "show_recording") {
        var r = config.recordings[a.recording_id];
        var data = Object.assign({}, r.data, { at_sec: a.at_sec });
        setTimeout(function() { ok(msg.id, data); }, r.delay || 0);
      } else if (name === "app_media_url") ok(msg.id, { url: null, kind: null, expires_at: null, reason: "no media" });
      else reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: "unexpected" }] } });
    } else if (msg.id !== undefined) {
      reply({ jsonrpc: "2.0", id: msg.id, result: {} });
    }
  });
};
</script></body></html>`;

export async function run() {
  const view = renderMcpSearchView();
  const server = http.createServer((req, res) => {
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(req.url === "/view" ? view : HOST);
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
    return { page, frame, errors };
  }
  const calls = (page, name) => page.evaluate((n) =>
    window.__log.filter((m) => m.method === "tools/call" && m.params.name === n).map((m) => m.params.arguments), name);
  const hits = (frame) => frame.evaluate(() =>
    Array.from(document.querySelectorAll("[data-hit] .hit-text")).map((e) => e.textContent));
  const playerState = (frame) => frame.evaluate(() => ({
    listHidden: document.querySelector("[data-search-root]").hidden,
    playerHidden: document.querySelector("[data-player-root]").hidden,
    title: document.querySelector("[data-player-root] [data-title]").textContent,
    cited: document.querySelector("[data-player-root] .cue.cited")?.textContent ?? null,
  }));

  const recordings = {
    [A]: { data: recordingData(A, "Alpha", null) },
    [B]: { data: recordingData(B, "Beta", null) },
    [C]: { data: recordingData(C, "Gamma", null) },
  };

  try {
    // --- List, more, open, back, open another ---
    {
      const { page, frame, errors } = await start({
        args: { query: "budget", limit: 2 },
        result: { content: [{ type: "text", text: "json" }], structuredContent: {
          query: "budget", threshold: 0.45, total: 3, returned: 2, offset: 0, truncated: false,
          hits: [hit(A, "Alpha", 40, "<b>budget</b> for Q3", ["Вася"]), hit(B, "Beta", 12, "the budget is fixed")] } },
        more: { query: "budget", threshold: 0.45, total: 3, returned: 1, offset: 2, truncated: false,
          hits: [hit(C, "Gamma", 80, "budget again")] },
        recordings,
        hostContext: { locale: "en" },
      });
      await frame.waitForFunction(() => document.querySelectorAll("[data-hit]").length === 2, null, { timeout: 3000 })
        .catch(() => fail("the two hits never rendered"));
      const head = await frame.evaluate(() => ({
        title: document.querySelector("[data-search-title]").textContent,
        meta: document.querySelector("[data-search-meta]").textContent,
        more: !document.querySelector("[data-more]").hidden,
      }));
      if (head.title !== "«budget»" || head.meta !== "3 passages") fail(`header ${JSON.stringify(head)}`);
      if (!head.more) fail("with total 3 and 2 shown, 'Show more' should be offered");
      // Transcript text is data: markup in it shows as text.
      const first = await hits(frame);
      if (first[0] !== "<b>budget</b> for Q3") fail(`hit text should be literal: ${JSON.stringify(first[0])}`);
      if (await frame.evaluate(() => !!document.querySelector("[data-hit] b"))) fail("hit text was parsed as HTML");

      await frame.click("[data-more]");
      await frame.waitForFunction(() => document.querySelectorAll("[data-hit]").length === 3, null, { timeout: 3000 })
        .catch(() => {});
      const moreCalls = await calls(page, "search_transcripts");
      if (JSON.stringify(moreCalls) !== JSON.stringify([{ query: "budget", limit: 2, offset: 2 }])) {
        fail(`'Show more' should repeat the search from offset 2: ${JSON.stringify(moreCalls)}`);
      }
      if (JSON.stringify(await hits(frame)) !== JSON.stringify(["<b>budget</b> for Q3", "the budget is fixed", "budget again"])) {
        fail(`after 'Show more': ${JSON.stringify(await hits(frame))}`);
      }
      if (await frame.evaluate(() => !document.querySelector("[data-more]").hidden)) fail("all 3 shown: 'Show more' should go");

      // Open the first passage: the player, on its moment.
      await frame.click("[data-hit]");
      await frame.waitForFunction(() => document.querySelector("[data-player-root] .cue.cited"), null, { timeout: 3000 })
        .catch(() => {});
      let st = await playerState(frame);
      if (!st.listHidden || st.playerHidden) fail(`opening a passage should show the player: ${JSON.stringify(st)}`);
      if (st.title !== "Alpha") fail(`player title ${JSON.stringify(st.title)}`);
      if (st.cited !== "Alpha line 10") fail(`the passage at 40 s should be cited ("Alpha line 10"), got ${st.cited}`);
      const opened = await calls(page, "show_recording");
      if (JSON.stringify(opened) !== JSON.stringify([{ recording_id: A, at_sec: 40 }])) {
        fail(`show_recording should get the hit's recording and start: ${JSON.stringify(opened)}`);
      }

      // Back, and the list is as it was.
      await frame.click("[data-back]");
      st = await playerState(frame);
      if (st.listHidden || !st.playerHidden) fail(`'Back' should return to the list: ${JSON.stringify(st)}`);
      if ((await hits(frame)).length !== 3) fail("the list should keep its 3 passages after 'Back'");

      // Another passage replaces the first recording entirely.
      await frame.click("[data-hit]:nth-child(3)", { timeout: 3000 })
        .catch(() => fail("after 'Back' the passages could not be clicked"));
      await frame.waitForFunction(() => document.querySelector("[data-player-root] .cue.cited")?.textContent === "Gamma line 20",
        null, { timeout: 3000 }).catch(() => {});
      st = await playerState(frame);
      if (st.title !== "Gamma" || st.cited !== "Gamma line 20") fail(`second passage: ${JSON.stringify(st)}`);
      const alphaLeft = await frame.evaluate(() =>
        Array.from(document.querySelectorAll("[data-player-root] .cue")).some((c) => c.textContent.startsWith("Alpha")));
      if (alphaLeft) fail("lines of the first recording are still in the player");
      if (errors.length) fail(`JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A slow answer for an abandoned passage does not win ---
    {
      const slow = { ...recordings, [A]: { data: recordingData(A, "Alpha", null), delay: 700 } };
      const { page, frame, errors } = await start({
        args: { query: "budget" },
        result: { content: [{ type: "text", text: "json" }], structuredContent: {
          query: "budget", threshold: 0.45, total: 2, returned: 2, offset: 0, truncated: false,
          hits: [hit(A, "Alpha", 40, "slow one"), hit(B, "Beta", 12, "fast one")] } },
        recordings: slow,
      });
      await frame.waitForFunction(() => document.querySelectorAll("[data-hit]").length === 2, null, { timeout: 3000 });
      await frame.click("[data-hit]");          // Alpha, answers late
      await frame.click("[data-back]");
      await frame.click("[data-hit]:nth-child(2)", { timeout: 3000 }); // Beta, answers at once
      await page.waitForTimeout(1000);          // Alpha's answer has arrived by now
      const st = await playerState(frame);
      if (st.title !== "Beta") fail(`the passage opened last should be shown, got ${JSON.stringify(st.title)}`);
      const alpha = await frame.evaluate(() =>
        Array.from(document.querySelectorAll("[data-player-root] .cue")).some((c) => c.textContent.startsWith("Alpha")));
      if (alpha) fail("a late answer for an abandoned passage was rendered");
      if (errors.length) fail(`race: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Empty and error results ---
    {
      const { page, frame } = await start({
        args: { query: "nothing" },
        result: { content: [{ type: "text", text: "json" }], structuredContent: {
          query: "nothing", threshold: 0.45, total: 0, returned: 0, offset: 0, truncated: false, hits: [] } },
        recordings, hostContext: { locale: "ru" },
      });
      await frame.waitForFunction(() => document.querySelector("[data-search-status]").textContent !== "", null,
        { timeout: 3000 }).catch(() => {});
      const st = await frame.evaluate(() => ({
        status: document.querySelector("[data-search-status]").textContent,
        more: !document.querySelector("[data-more]").hidden,
      }));
      if (st.status !== "В ваших записях нет ничего достаточно близкого.") fail(`empty, ru: ${JSON.stringify(st.status)}`);
      if (st.more) fail("an empty result offers no 'Show more'");
      await page.close();
    }
    {
      const { page, frame } = await start({
        args: { query: "x" },
        result: { isError: true, content: [{ type: "text", text: "invalid offset" }] },
        recordings,
      });
      await frame.waitForFunction(() => document.querySelector("[data-search-status]").textContent === "invalid offset",
        null, { timeout: 3000 }).catch(() => {});
      const status = await frame.evaluate(() => document.querySelector("[data-search-status]").textContent);
      if (status !== "invalid offset") fail(`an error result should show its text: ${JSON.stringify(status)}`);
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

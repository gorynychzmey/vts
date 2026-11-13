// vts-vtma.2: the MCP Apps player view against a host.
//
// The page here plays the HOST: it loads the real `ui://vts/player` document
// (rendered by the server's own function) into a sandboxed iframe, answers
// ui/initialize and tools/call, sends the tool result once the view says it is
// initialized — the spec forbids anything earlier — and records every message
// the view sends. So this drives the hand-written bridge through the protocol
// as a host speaks it, and the view's rendering through the shared core.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderMcpPlayerView } from "../player-page.mjs";

export const name = "mcp-player-view";

const REC = "33333333-4444-5555-6666-777777777777";

// n blocks, one sentence each, sentence i starting at 2i seconds.
function blocks(from, n) {
  return Array.from({ length: n }, (_, k) => {
    const i = from + k;
    return { label: `Голос ${i % 2 + 1}`, sentences: [{ start: i * 2, text: `sentence ${i}` }] };
  });
}

const HOST = `<!doctype html><html><body>
<script>
window.__log = [];
window.__start = function(config) {
  var frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.style.width = "600px"; frame.style.height = "500px";
  frame.src = "/view";
  document.body.appendChild(frame);
  var initialized = false;
  function reply(msg) { frame.contentWindow.postMessage(msg, "*"); }
  window.addEventListener("message", function(event) {
    if (event.source !== frame.contentWindow) return;
    var msg = event.data;
    window.__log.push({ method: msg.method, params: msg.params, initialized: initialized });
    if (msg.method === "ui/initialize") {
      reply({ jsonrpc: "2.0", id: msg.id, result: {
        protocolVersion: "2026-01-26", hostInfo: { name: "stub", version: "1" },
        hostCapabilities: {}, hostContext: config.hostContext || {} } });
    } else if (msg.method === "ui/notifications/initialized") {
      initialized = true;
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: config.args } });
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: config.result });
    } else if (msg.method === "tools/call") {
      var page = config.pages && config.pages[msg.params.arguments.cursor];
      if (config.failCalls || !page) {
        reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: "nope" }] } });
      } else {
        reply({ jsonrpc: "2.0", id: msg.id, result: { content: [{ type: "text", text: "page" }], structuredContent: page } });
      }
    } else if (msg.id !== undefined) {
      reply({ jsonrpc: "2.0", id: msg.id, result: {} });
    }
  });
  window.__frame = frame;
};
</script></body></html>`;

export async function run() {
  const view = renderMcpPlayerView();
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
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base + "/", { waitUntil: "load" });
    await page.evaluate((c) => window.__start(c), config);
    await page.waitForFunction(() => window.__frame && window.__frame.contentWindow, null, { timeout: 3000 });
    const frame = page.frames().find((f) => f.url().endsWith("/view"))
      || await (await page.waitForSelector("iframe")).contentFrame();
    return { page, frame, errors };
  }
  const log = (page) => page.evaluate(() => window.__log);
  const cueTexts = (frame) => frame.evaluate(() =>
    Array.from(document.querySelectorAll(".cue")).map((c) => [c.getAttribute("data-cue"), c.textContent]));

  try {
    // --- Handshake, paging, positioning on at_sec, model context, size ---
    {
      const first = { recording_id: REC, title: "Weekly call", duration_sec: 600, people: ["Вася"],
        at_sec: 250, blocks: blocks(0, 100), next_cursor: "100" };
      const { page, frame, errors } = await start({
        args: { recording_id: REC, at_sec: 250 },
        result: { content: [{ type: "text", text: "summary" }], structuredContent: first },
        pages: { "100": { blocks: blocks(100, 100), next_cursor: "200" }, "200": { blocks: blocks(200, 20), next_cursor: null } },
        hostContext: { theme: "dark", locale: "ru-RU" },
      });
      await frame.waitForFunction(() => document.querySelectorAll(".cue").length === 220, null, { timeout: 5000 })
        .catch(() => {});

      const msgs = await log(page);
      const init = msgs.find((m) => m.method === "ui/initialize");
      if (!init) fail("the view never sent ui/initialize");
      else if (init.params.protocolVersion !== "2026-01-26") fail(`protocolVersion ${init.params.protocolVersion}`);
      const order = msgs.map((m) => m.method);
      if (order.indexOf("ui/notifications/initialized") < order.indexOf("ui/initialize")) {
        fail(`initialized must follow the initialize reply: ${JSON.stringify(order.slice(0, 4))}`);
      }
      // Media has its own tool (covered by mcp-player-media); here, the pages.
      const calls = msgs.filter((m) => m.method === "tools/call" && m.params.name === "app_player_blocks")
        .map((m) => [m.params.name, m.params.arguments.cursor]);
      if (JSON.stringify(calls) !== JSON.stringify([["app_player_blocks", "100"], ["app_player_blocks", "200"]])) {
        fail(`the view should fetch exactly the two remaining pages: ${JSON.stringify(calls)}`);
      }
      if (msgs.some((m) => m.method === "tools/call" && m.params.arguments.recording_id !== REC)) {
        fail("a page was requested for another recording");
      }

      const cues = await cueTexts(frame);
      const expected = Array.from({ length: 220 }, (_, i) => [String(i), `sentence ${i}`]);
      if (JSON.stringify(cues) !== JSON.stringify(expected)) {
        fail(`all 220 sentences, numbered in order across pages: got ${cues.length}, first ${JSON.stringify(cues.slice(0, 2))}`);
      }
      // at_sec=250 lies on page 2 (sentence 125 starts at 250 s): the view
      // positions there once that page has arrived, not on a guess from page 1.
      const cited = await frame.evaluate(() => document.querySelector(".cue.cited")?.textContent ?? null);
      if (cited !== "sentence 125") fail(`at_sec=250 should cite "sentence 125", got ${cited}`);
      // Smooth scrolling takes a moment; wait for it rather than sample once.
      const inView = await frame.waitForFunction(() => {
        const box = document.querySelector(".transcript").getBoundingClientRect();
        const el = document.querySelector(".cue.cited")?.getBoundingClientRect();
        return !!el && el.top >= box.top - 1 && el.bottom <= box.bottom + 1;
      }, null, { timeout: 3000 }).then(() => true, () => false);
      if (!inView) fail("the cited sentence was not scrolled into view");

      const head = await frame.evaluate(() => ({
        title: document.querySelector("[data-title]").textContent,
        meta: document.querySelector("[data-meta]").textContent,
        status: document.querySelector("[data-status]").textContent,
        theme: document.documentElement.getAttribute("data-theme"),
      }));
      if (head.title !== "Weekly call") fail(`title ${JSON.stringify(head.title)}`);
      if (head.meta !== "10:00 · Участники: Вася") fail(`meta line in ru ${JSON.stringify(head.meta)}`);
      if (head.status !== "") fail(`the loading status should clear once all pages are in: ${JSON.stringify(head.status)}`);
      if (head.theme !== "dark") fail(`hostContext.theme=dark not applied: ${head.theme}`);

      // Picking a sentence tells the model what the user is looking at.
      await frame.click('.cue[data-cue="3"]');
      await page.waitForFunction(() => window.__log.some((m) => m.method === "ui/update-model-context"), null,
        { timeout: 3000 }).catch(() => {});
      const ctx = (await log(page)).find((m) => m.method === "ui/update-model-context");
      const text = ctx?.params?.content?.[0]?.text || "";
      if (!text.includes("sentence 3") || !text.includes("0:06") || !text.includes(REC)) {
        fail(`ui/update-model-context should name the sentence, its time and the recording: ${JSON.stringify(text)}`);
      }

      const size = (await log(page)).filter((m) => m.method === "ui/notifications/size-changed").pop();
      if (!size || !(size.params.height > 0)) fail(`the view should report its size: ${JSON.stringify(size)}`);
      if (errors.length) fail(`JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- An empty transcript says so, in the host's language ---
    {
      const { page, frame, errors } = await start({
        args: { recording_id: REC },
        result: { content: [{ type: "text", text: "s" }],
          structuredContent: { recording_id: REC, title: "x", blocks: [], next_cursor: null } },
        hostContext: { locale: "de-DE" },
      });
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent.includes("Transkript"),
        null, { timeout: 3000 }).catch(() => {});
      const status = await frame.evaluate(() => document.querySelector("[data-status]").textContent);
      if (status !== "Diese Aufnahme hat noch kein Transkript.") fail(`empty transcript, de: ${JSON.stringify(status)}`);
      if (errors.length) fail(`empty: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A failed tool result and a failed page are reported, not hidden ---
    {
      const { page, frame } = await start({
        args: { recording_id: REC },
        result: { isError: true, content: [{ type: "text", text: "Recording not found" }] },
      });
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent === "Recording not found",
        null, { timeout: 3000 }).catch(() => {});
      const status = await frame.evaluate(() => document.querySelector("[data-status]").textContent);
      if (status !== "Recording not found") fail(`an error result should show its text: ${JSON.stringify(status)}`);
      await page.close();
    }
    {
      const { page, frame } = await start({
        args: { recording_id: REC },
        result: { content: [{ type: "text", text: "s" }],
          structuredContent: { recording_id: REC, title: "x", blocks: blocks(0, 3), next_cursor: "3" } },
        failCalls: true,
      });
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent.includes("could not"),
        null, { timeout: 3000 }).catch(() => {});
      const state = await frame.evaluate(() => ({
        status: document.querySelector("[data-status]").textContent,
        cues: document.querySelectorAll(".cue").length,
      }));
      if (state.status !== "The transcript could not be loaded.") fail(`a failed page: ${JSON.stringify(state.status)}`);
      // What did arrive stays readable.
      if (state.cues !== 3) fail(`the first page should stay on screen after a failed page: ${state.cues} cues`);
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

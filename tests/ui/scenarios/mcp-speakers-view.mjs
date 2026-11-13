// vts-vtma.6: the MCP Apps speakers view against a host.
//
// The page plays the HOST: it loads the real `ui://vts/speakers` document into
// a sandboxed iframe, sends a show_speakers result, and answers the tools the
// view calls — app_speaker_preview_url (a clip served here), app_resolve_speakers
// (recorded, then the refreshed show_speakers). Covered: the initial choices
// follow the web dialog's rule, a new person needs a name, the save sends the
// plain choices, a double click saves once, noise, listening, "save and
// continue" while the task waits, a read-only recording, and a refused save.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderMcpSpeakersView } from "../player-page.mjs";

export const name = "mcp-speakers-view";

const REC = "eeeeeeee-0000-0000-0000-000000000005";
const ANNA = "11111111-aaaa-0000-0000-000000000001";
const BORIS = "22222222-bbbb-0000-0000-000000000002";
const PEOPLE = [{ id: ANNA, name: "Anna" }, { id: BORIS, name: "Boris" }];

function voices(decided = {}) {
  return [
    { label: "SPEAKER_00", display_label: "Голос 1", outcome: "auto", matched_speaker_id: ANNA,
      decided_speaker_id: decided.s0 ?? null, decided_name: decided.s0name ?? null, decided_is_noise: decided.s0noise ?? null,
      options: [{ speaker_id: ANNA, name: "Anna", distance: 0.2 }, { speaker_id: BORIS, name: "Boris", distance: null }], clips: 1 },
    { label: "SPEAKER_01", display_label: "Голос 2", outcome: "miss", matched_speaker_id: null,
      decided_speaker_id: decided.s1 ?? null, decided_name: decided.s1name ?? null, decided_is_noise: decided.s1noise ?? null,
      options: [{ speaker_id: ANNA, name: "Anna", distance: null }, { speaker_id: BORIS, name: "Boris", distance: null }], clips: 0 },
  ];
}
const view = (extra = {}) => ({ recording_id: REC, title: "Weekly call", voices: voices(), people: PEOPLE,
  editable: true, reason: null, awaiting_input: false, ...extra });

const HOST = `<!doctype html><html><body>
<script>
window.__log = [];
window.__start = function(config) {
  var frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.style.width = "700px"; frame.style.height = "600px";
  frame.src = "/view";
  document.body.appendChild(frame);
  function reply(msg) { frame.contentWindow.postMessage(msg, "*"); }
  function ok(id, data) { reply({ jsonrpc: "2.0", id: id, result: { content: [], structuredContent: data } }); }
  window.addEventListener("message", function(event) {
    if (event.source !== frame.contentWindow) return;
    var msg = event.data;
    window.__log.push({ method: msg.method, params: msg.params });
    if (msg.method === "ui/initialize") {
      reply({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: "2026-01-26",
        hostCapabilities: {}, hostContext: config.hostContext || {} } });
    } else if (msg.method === "ui/notifications/initialized") {
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: {} } });
      reply({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { content: [], structuredContent: config.initial } });
    } else if (msg.method === "tools/call") {
      var n = msg.params.name;
      if (n === "app_speaker_preview_url") ok(msg.id, { url: location.origin + "/clip.wav", expires_at: 0, reason: null });
      else if (n === "app_resolve_speakers") {
        if (config.saveError) reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: config.saveError }] } });
        else setTimeout(function() { ok(msg.id, { results: {}, continued: msg.params.arguments.continue_task }); }, 300);
      } else if (n === "show_speakers") ok(msg.id, config.after);
      else reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: "unexpected" }] } });
    } else if (msg.id !== undefined) {
      reply({ jsonrpc: "2.0", id: msg.id, result: {} });
    }
  });
};
</script></body></html>`;

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

export async function run() {
  const html = renderMcpSpeakersView();
  const wav = silentWav(2);
  const server = http.createServer((req, res) => {
    if (req.url === "/view") { res.setHeader("Content-Type", "text/html; charset=utf-8"); res.end(html); return; }
    if (req.url === "/clip.wav") { res.setHeader("Content-Type", "audio/wav"); res.end(wav); return; }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end(HOST);
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
    const frame = await (await page.waitForSelector("iframe")).contentFrame();
    await frame.waitForFunction(() => document.querySelectorAll("[data-voice]").length > 0, null, { timeout: 3000 })
      .catch(() => {});
    return { page, frame, errors };
  }
  const calls = (page, n) => page.evaluate((name) =>
    window.__log.filter((m) => m.method === "tools/call" && m.params.name === name).map((m) => m.params.arguments), n);
  const voiceState = (frame) => frame.evaluate(() =>
    Array.from(document.querySelectorAll("[data-voice]")).map((li) => ({
      label: li.getAttribute("data-voice"),
      choice: li.querySelector("[data-choice]")?.value ?? null,
      remember: li.querySelector("[data-remember]") ? { checked: li.querySelector("[data-remember]").checked,
        hidden: li.querySelector("[data-remember]").parentElement.hidden } : null,
      now: li.querySelector("[data-now]").textContent,
    })));

  try {
    // --- Initial choices, name required, save, refresh, model context ---
    {
      const { page, frame, errors } = await start({
        initial: view(),
        after: view({ voices: voices({ s0: ANNA, s0name: "Anna", s0noise: false, s1name: null, s1noise: false }) }),
        hostContext: { locale: "ru" },
      });
      let st = await voiceState(frame);
      // auto-matched voice: its match preselected, "remember" on; unmatched: anonymous.
      if (st[0].choice !== ANNA || !st[0].remember.checked || st[0].remember.hidden) fail(`auto voice initial: ${JSON.stringify(st[0])}`);
      if (st[1].choice !== "__anonymous__" || !st[1].remember.hidden) fail(`miss voice initial: ${JSON.stringify(st[1])}`);
      if (st[0].now !== "сейчас: не опознан") fail(`current binding line in ru: ${JSON.stringify(st[0].now)}`);

      // A new person with no name is not saved.
      await frame.selectOption('[data-voice="SPEAKER_01"] [data-choice]', "__new__");
      await frame.click("[data-save]");
      const status = await frame.evaluate(() => document.querySelector("[data-status]").textContent);
      if (status !== "Дайте новому человеку имя или выберите «Аноним».") fail(`nameless new person: ${JSON.stringify(status)}`);
      if ((await calls(page, "app_resolve_speakers")).length) fail("a nameless new person must not be saved");

      await frame.fill('[data-voice="SPEAKER_01"] [data-new-name]', "Dora");
      await frame.check('[data-voice="SPEAKER_01"] [data-noise]');
      // Double click: one save.
      await frame.click("[data-save]");
      await frame.click("[data-save]", { force: true }).catch(() => {});
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent === "Сохранено.", null,
        { timeout: 3000 }).catch(() => {});
      const saves = await calls(page, "app_resolve_speakers");
      if (saves.length !== 1) fail(`one save expected, got ${saves.length}`);
      const expected = { recording_id: REC, voices: {
        SPEAKER_00: { is_noise: false, kind: "person", speaker_id: ANNA, add_fragment: true },
        SPEAKER_01: { is_noise: true, kind: "new", new_name: "Dora" } }, continue_task: false };
      if (JSON.stringify(saves[0]) !== JSON.stringify(expected)) fail(`save payload: ${JSON.stringify(saves[0])}`);
      if ((await calls(page, "show_speakers")).length !== 1) fail("after saving, the view should reload the voices");
      st = await voiceState(frame);
      if (st[0].now !== "сейчас: Anna") fail(`after save the current binding should show: ${JSON.stringify(st[0].now)}`);
      const ctx = (await page.evaluate(() => window.__log.find((m) => m.method === "ui/update-model-context")))?.params?.content?.[0]?.text || "";
      if (!ctx.includes("Голос 1 = Anna") || !ctx.includes(REC)) fail(`model context: ${JSON.stringify(ctx)}`);
      if (errors.length) fail(`JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- Listening, and "save and continue" while the task waits ---
    {
      const { page, frame, errors } = await start({ initial: view({ awaiting_input: true }), after: view({ awaiting_input: false }) });
      const listenButtons = await frame.evaluate(() =>
        Array.from(document.querySelectorAll("[data-voice]")).map((li) => !!li.querySelector("[data-listen]")));
      if (JSON.stringify(listenButtons) !== JSON.stringify([true, false])) fail(`listen only where a clip exists: ${JSON.stringify(listenButtons)}`);
      await frame.click('[data-voice="SPEAKER_00"] [data-listen]');
      await frame.waitForSelector('[data-voice="SPEAKER_00"] audio', { timeout: 3000 }).catch(() => {});
      const src = await frame.evaluate(() => document.querySelector('[data-voice="SPEAKER_00"] audio')?.src ?? null);
      if (!src || !src.endsWith("/clip.wav")) fail(`the clip should load from its signed link: ${src}`);
      const asked = await calls(page, "app_speaker_preview_url");
      if (JSON.stringify(asked) !== JSON.stringify([{ recording_id: REC, label: "SPEAKER_00", index: 0 }])) {
        fail(`clip link request: ${JSON.stringify(asked)}`);
      }
      if (await frame.evaluate(() => document.querySelector("[data-save-continue]").hidden)) fail("a waiting task should offer 'save and continue'");
      await frame.click("[data-save-continue]");
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent === "Saved.", null, { timeout: 3000 }).catch(() => {});
      const saves = await calls(page, "app_resolve_speakers");
      if (saves.length !== 1 || saves[0].continue_task !== true) fail(`'save and continue' should continue the task: ${JSON.stringify(saves)}`);
      if (errors.length) fail(`listen/continue: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A saved decision wins over the matcher's guess, as in the web dialog ---
    {
      const { page, frame } = await start({ initial: view({ voices: voices({ s0: BORIS, s0name: "Boris", s0noise: false }) }) });
      const st = await voiceState(frame);
      if (st[0].choice !== BORIS) fail(`a saved binding to Boris should be preselected over the auto-match: ${st[0].choice}`);
      await page.close();
    }

    // --- Read-only, and a refused save ---
    {
      const { page, frame } = await start({ initial: view({ editable: false, reason: "task_deleted" }) });
      const st = await frame.evaluate(() => ({
        selects: document.querySelectorAll("[data-choice]").length,
        actions: document.querySelector("[data-actions]").hidden,
        note: document.querySelector("[data-note]").textContent,
      }));
      if (st.selects || !st.actions) fail(`a read-only recording offers no editing: ${JSON.stringify(st)}`);
      if (!st.note.includes("deleted")) fail(`read-only note: ${JSON.stringify(st.note)}`);
      await page.close();
    }
    {
      const { page, frame } = await start({ initial: view(), saveError: "cannot_resolve_speakers" });
      await frame.click("[data-save]");
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent === "cannot_resolve_speakers",
        null, { timeout: 3000 }).catch(() => {});
      const st = await frame.evaluate(() => ({
        status: document.querySelector("[data-status]").textContent,
        disabled: document.querySelector("[data-save]").disabled,
      }));
      if (st.status !== "cannot_resolve_speakers") fail(`a refused save should show why: ${JSON.stringify(st.status)}`);
      if (st.disabled) fail("after a refused save the button should work again");
      // ...and really does: a second try goes out.
      await frame.click("[data-save]");
      await page.waitForTimeout(300);
      if ((await calls(page, "app_resolve_speakers")).length !== 2) fail("after a refused save, saving again should send a second request");
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

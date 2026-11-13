// vts-vtma.5: the MCP Apps summary view against a host.
//
// The page plays the HOST: it loads the real `ui://vts/summary` document into
// a sandboxed iframe, sends a show_summary result and answers
// get_recording_prompt_result when the user switches results. Covered: the
// markdown is rendered as structure and its text stays text, the switcher
// (only with more than one result, labelled in the host's language), a
// switch fetching and showing another result and telling the model, a slow
// earlier switch not overwriting a later one, and an error result.
import http from "node:http";

import { launch } from "../harness.mjs";
import { renderMcpSummaryView } from "../player-page.mjs";

export const name = "mcp-summary-view";

const REC = "dddddddd-0000-0000-0000-000000000004";
const SUMMARY = [
  "# Итог встречи",
  "",
  "Обсудили **бюджет** и `Q3`.",
  "",
  "- первый пункт",
  "- второй <script>window.__pwned = 1</script> пункт",
  "  - вложенный",
  "",
  "1. шаг один",
  "2. шаг два",
].join("\n");

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
    } else if (msg.method === "tools/call" && msg.params.name === "get_recording_prompt_result") {
      var r = config.results[msg.params.arguments.ref];
      setTimeout(function() {
        reply({ jsonrpc: "2.0", id: msg.id, result: { content: [{ type: "text", text: "r" }],
          structuredContent: { task_id: null, source: "user", id: "x", content: r.content } } });
      }, r.delay || 0);
    } else if (msg.id !== undefined) {
      reply({ jsonrpc: "2.0", id: msg.id, result: {} });
    }
  });
};
</script></body></html>`;

export async function run() {
  const view = renderMcpSummaryView();
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
    const frame = await (await page.waitForSelector("iframe")).contentFrame();
    // An error result has no title to wait for.
    if (!config.result.isError) {
      await frame.waitForFunction(() => document.querySelector("[data-title]").textContent !== "", null, { timeout: 3000 })
        .catch(() => {});
    }
    return { page, frame, errors };
  }
  const results = [{ ref: "system:summary", name: "Summary" }, { ref: "user:m1", name: "Memo" }, { ref: "user:m2", name: "Risks" }];
  const result = (ref, markdown, list) => ({ content: [{ type: "text", text: "s" }], structuredContent: {
    recording_id: REC, title: "Weekly call", ref, markdown, results: list } });

  try {
    // --- Rendering, switcher, a switch ---
    {
      const { page, frame, errors } = await start({
        result: result("system:summary", SUMMARY, results),
        results: { "user:m1": { content: "Memo **text**" }, "user:m2": { content: "Risks" } },
        hostContext: { locale: "ru" },
      });
      const md = await frame.evaluate(() => {
        const a = document.querySelector("[data-markdown]");
        return {
          h2: a.querySelector("h2")?.textContent ?? null,
          strong: a.querySelector("p strong")?.textContent ?? null,
          code: a.querySelector("p code")?.textContent ?? null,
          ul: Array.from(a.querySelectorAll(":scope > ul > li")).map((li) => li.firstChild.textContent.trim()),
          nested: a.querySelector("ul ul li")?.textContent ?? null,
          ol: Array.from(a.querySelectorAll(":scope > ol > li")).map((li) => li.textContent),
          scripts: a.querySelectorAll("script").length,
        };
      });
      if (md.h2 !== "Итог встречи") fail(`'# heading' should render as a heading: ${JSON.stringify(md.h2)}`);
      if (md.strong !== "бюджет" || md.code !== "Q3") fail(`inline bold/code: ${JSON.stringify(md)}`);
      if (JSON.stringify(md.ul) !== JSON.stringify(["первый пункт", "второй <script>window.__pwned = 1</script> пункт"])) {
        fail(`bullet list, markup kept as text: ${JSON.stringify(md.ul)}`);
      }
      if (md.nested !== "вложенный") fail(`nested list item: ${JSON.stringify(md.nested)}`);
      if (JSON.stringify(md.ol) !== JSON.stringify(["шаг один", "шаг два"])) fail(`numbered list: ${JSON.stringify(md.ol)}`);
      if (md.scripts || (await frame.evaluate(() => window.__pwned))) fail("markup in a result was executed");

      const sw = await frame.evaluate(() => ({
        hidden: document.querySelector("[data-switcher]").hidden,
        buttons: Array.from(document.querySelectorAll("[data-switcher] button")).map((b) => [b.textContent, b.getAttribute("aria-pressed")]),
      }));
      if (sw.hidden) fail("with three results the switcher should show");
      if (JSON.stringify(sw.buttons) !== JSON.stringify([["Саммари", "true"], ["Memo", "false"], ["Risks", "false"]])) {
        fail(`switcher buttons: ${JSON.stringify(sw.buttons)}`);
      }

      await frame.click('[data-ref="user:m1"]');
      await frame.waitForFunction(() => document.querySelector("[data-markdown] strong")?.textContent === "text", null,
        { timeout: 3000 }).catch(() => {});
      const after = await frame.evaluate(() => ({
        text: document.querySelector("[data-markdown]").textContent,
        pressed: document.querySelector('[data-ref="user:m1"]').getAttribute("aria-pressed"),
      }));
      if (after.text !== "Memo text" || after.pressed !== "true") fail(`after switching to Memo: ${JSON.stringify(after)}`);
      const log = await page.evaluate(() => window.__log);
      const fetches = log.filter((m) => m.method === "tools/call").map((m) => m.params.arguments);
      if (JSON.stringify(fetches) !== JSON.stringify([{ recording_id: REC, ref: "user:m1" }])) {
        fail(`a switch should fetch exactly that result: ${JSON.stringify(fetches)}`);
      }
      const ctx = log.find((m) => m.method === "ui/update-model-context")?.params?.content?.[0]?.text || "";
      if (!ctx.includes("Memo") || !ctx.includes("user:m1") || !ctx.includes(REC)) {
        fail(`the model should hear what the user is reading: ${JSON.stringify(ctx)}`);
      }
      if (errors.length) fail(`JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- A slow earlier switch does not overwrite a later one ---
    {
      const { page, frame, errors } = await start({
        result: result("system:summary", "Summary", results),
        results: { "user:m1": { content: "slow memo", delay: 600 }, "user:m2": { content: "fast risks" } },
      });
      await frame.click('[data-ref="user:m1"]');
      await frame.click('[data-ref="user:m2"]');
      await page.waitForTimeout(900);
      const shown = await frame.evaluate(() => ({
        text: document.querySelector("[data-markdown]").textContent,
        pressed: Array.from(document.querySelectorAll('[aria-pressed="true"]')).map((b) => b.getAttribute("data-ref")),
      }));
      if (shown.text !== "fast risks") fail(`the result chosen last should be shown: ${JSON.stringify(shown.text)}`);
      if (JSON.stringify(shown.pressed) !== JSON.stringify(["user:m2"])) fail(`pressed: ${JSON.stringify(shown.pressed)}`);
      if (errors.length) fail(`race: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }

    // --- One result: no switcher. An error result: its text ---
    {
      const { page, frame } = await start({
        result: result("system:summary", "Only one", [results[0]]),
        results: {},
      });
      if (!(await frame.evaluate(() => document.querySelector("[data-switcher]").hidden))) {
        fail("a single result needs no switcher");
      }
      await page.close();
    }
    {
      const { page, frame } = await start({
        result: { isError: true, content: [{ type: "text", text: "No prompt result 'user:x'; available: system:summary" }] },
        results: {},
      });
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent.startsWith("No prompt result"),
        null, { timeout: 3000 }).catch(() => {});
      const status = await frame.evaluate(() => document.querySelector("[data-status]").textContent);
      if (!status.startsWith("No prompt result")) fail(`an error result should show its text: ${JSON.stringify(status)}`);
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

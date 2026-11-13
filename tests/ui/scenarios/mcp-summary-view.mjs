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
    } else if (msg.method === "tools/call" && msg.params.name === "list_prompts") {
      reply({ jsonrpc: "2.0", id: msg.id, result: { content: [], structuredContent: { result: config.prompts || [] } } });
    } else if (msg.method === "tools/call" && msg.params.name === "regenerate_summary") {
      if (config.regenError) {
        reply({ jsonrpc: "2.0", id: msg.id, result: { isError: true, content: [{ type: "text", text: config.regenError }] } });
      } else {
        reply({ jsonrpc: "2.0", id: msg.id, result: { content: [], structuredContent: {
          task_id: "t-1", status: "queued", prompts: msg.params.arguments.prompts } } });
      }
    } else if (msg.method === "tools/call" && msg.params.name === "get_status") {
      var st = config.statuses.shift() || config.finalStatus || "completed";
      reply({ jsonrpc: "2.0", id: msg.id, result: { content: [], structuredContent: { task_id: "t-1", status: st } } });
    } else if (msg.method === "tools/call" && msg.params.name === "show_summary") {
      reply({ jsonrpc: "2.0", id: msg.id, result: { content: [], structuredContent: config.after } });
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
  // Regeneration polls fast here: the server says 5 s, the stub 50 ms.
  const regenOk = { available: true, reason: null, current: ["system:summary", "user:m1"], poll_sec: 0.05 };
  const result = (ref, markdown, list, regenerate = { available: false, reason: "task_deleted", current: [], poll_sec: 5 }) => ({
    content: [{ type: "text", text: "s" }], structuredContent: {
      recording_id: REC, title: "Weekly call", ref, markdown, results: list, regenerate } });
  const prompts = [
    { source: "system", id: "summary", name: "Summary", editable: false },
    { source: "user", id: "m1", name: "Memo", editable: true },
    { source: "user", id: "m2", name: "Risks", editable: true },
  ];

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

    // --- Regenerate: confirm, re-run all at once, wait, show the new results ---
    {
      const { page, frame, errors } = await start({
        result: result("system:summary", "Old summary", results, regenOk),
        results: {}, prompts, statuses: ["queued", "running", "running"],
        after: { recording_id: REC, title: "Weekly call", ref: "system:summary", markdown: "New **summary**",
          results: results.slice(0, 2), regenerate: regenOk },
        hostContext: { locale: "ru" },
      });
      if (await frame.evaluate(() => document.querySelector("[data-regen]").hidden)) {
        fail("regenerate should be offered for a recording whose task can re-run");
      }
      await frame.click("[data-regen]");
      await frame.waitForFunction(() => document.querySelectorAll("[data-regen-prompts] input").length === 3, null,
        { timeout: 3000 }).catch(() => {});
      const panel = await frame.evaluate(() => ({
        warning: document.querySelector("[data-regen-warning]").textContent,
        boxes: Array.from(document.querySelectorAll("[data-regen-prompts] label")).map((l) =>
          [l.querySelector("input").value, l.querySelector("input").checked, l.textContent]),
      }));
      if (!panel.warning.includes("будут заменены")) fail(`the panel should warn that results are replaced: ${JSON.stringify(panel.warning)}`);
      // The current set is preselected, as in the web UI's dialog.
      if (JSON.stringify(panel.boxes) !== JSON.stringify([
        ["system:summary", true, "Саммари"], ["user:m1", true, "Memo"], ["user:m2", false, "Risks"]])) {
        fail(`prompt choices: ${JSON.stringify(panel.boxes)}`);
      }
      // Nothing chosen, nothing to run.
      await frame.click('[data-regen-prompts] input[value="system:summary"]');
      await frame.click('[data-regen-prompts] input[value="user:m1"]');
      if (!(await frame.evaluate(() => document.querySelector("[data-regen-run]").disabled))) {
        fail("with no prompt chosen, the run button should be disabled");
      }
      if ((await page.evaluate(() => window.__log.filter((m) => m.params?.name === "regenerate_summary").length)) !== 0) {
        fail("nothing may run before the user confirms");
      }
      await frame.click('[data-regen-prompts] input[value="system:summary"]');
      await frame.click('[data-regen-prompts] input[value="user:m2"]');
      await frame.click("[data-regen-run]");
      await frame.waitForFunction(() => document.querySelector("[data-markdown] strong")?.textContent === "summary", null,
        { timeout: 5000 }).catch(() => {});
      const log = await page.evaluate(() => window.__log);
      const regen = log.filter((m) => m.params?.name === "regenerate_summary").map((m) => m.params.arguments);
      if (JSON.stringify(regen) !== JSON.stringify([{ recording_id: REC, prompts: ["system:summary", "user:m2"] }])) {
        fail(`regenerate_summary should get the chosen set: ${JSON.stringify(regen)}`);
      }
      const polls = log.filter((m) => m.params?.name === "get_status").length;
      if (polls !== 4) fail(`the view should poll until completed (3 unfinished + 1 completed), polled ${polls}`);
      const shown = await frame.evaluate(() => ({
        text: document.querySelector("[data-markdown]").textContent,
        status: document.querySelector("[data-status]").textContent,
      }));
      if (shown.text !== "New summary" || shown.status !== "") fail(`after regeneration: ${JSON.stringify(shown)}`);
      const ctx = log.filter((m) => m.method === "ui/update-model-context").map((m) => m.params.content[0].text).join(" | ");
      if (!ctx.includes("re-ran") || !ctx.includes("user:m2")) fail(`the model should hear about the re-run: ${JSON.stringify(ctx)}`);
      if (errors.length) fail(`regenerate: JS errors ${JSON.stringify(errors)}`);
      await page.close();
    }
    {
      // While it runs the old text is gone; a failed run says so.
      const { page, frame } = await start({
        result: result("system:summary", "Old summary", results, regenOk),
        results: {}, prompts, statuses: ["running"], finalStatus: "failed", after: null,
      });
      await frame.click("[data-regen]");
      await frame.waitForFunction(() => document.querySelectorAll("[data-regen-prompts] input").length === 3, null, { timeout: 3000 });
      await frame.click("[data-regen-run]", { timeout: 3000 }).catch(() => fail("the run button could not be clicked"));
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent === "Re-running the prompts failed.",
        null, { timeout: 3000 }).catch(() => {});
      const st = await frame.evaluate(() => ({
        status: document.querySelector("[data-status]").textContent,
        md: document.querySelector("[data-markdown]").textContent,
      }));
      if (st.status !== "Re-running the prompts failed.") fail(`a failed run: ${JSON.stringify(st.status)}`);
      if (st.md !== "") fail(`the cleared results must not linger on screen: ${JSON.stringify(st.md)}`);
      await page.close();
    }
    {
      // A refusal from the server is shown, not swallowed.
      const { page, frame } = await start({
        result: result("system:summary", "Old summary", results, regenOk),
        results: {}, prompts, statuses: [], regenError: "The task is running; its prompts can be re-run once it has finished.",
      });
      await frame.click("[data-regen]");
      await frame.waitForFunction(() => document.querySelectorAll("[data-regen-prompts] input").length === 3, null, { timeout: 3000 });
      await frame.click("[data-regen-run]", { timeout: 3000 }).catch(() => fail("the run button could not be clicked"));
      await frame.waitForFunction(() => document.querySelector("[data-status]").textContent.startsWith("The task is running"),
        null, { timeout: 3000 }).catch(() => {});
      const status = await frame.evaluate(() => document.querySelector("[data-status]").textContent);
      if (!status.startsWith("The task is running")) fail(`a refused re-run: ${JSON.stringify(status)}`);
      await page.close();
    }

    // --- One result: no switcher, no regenerate for a lost task. An error result: its text ---
    {
      const { page, frame } = await start({
        result: result("system:summary", "Only one", [results[0]]),
        results: {},
      });
      if (!(await frame.evaluate(() => document.querySelector("[data-switcher]").hidden))) {
        fail("a single result needs no switcher");
      }
      if (!(await frame.evaluate(() => document.querySelector("[data-regen]").hidden))) {
        fail("a recording whose task was deleted must not offer regenerate");
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

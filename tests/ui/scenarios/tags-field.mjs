// The tag field (vts-qwcj): new-task form and the preset dialog.
//
// The field is one component (createTagField) used in three places; this
// scenario covers the two that create things — the composer and the preset
// editor — and what each one SENDS, because the server is where a mistake
// would land: a task created without its tags, or a preset saved without them
// (PATCH replaces the preset's options wholesale, so forgetting `tags` there
// wipes them).
//
//   * typing filters the suggestions from GET /api/tags by key ("биз" finds
//     "Бизнес-клуб"); arrows + Enter, comma and a suggestion click add; a name
//     that is not an existing tag shows as a `tag-chip--new` chip; a duplicate
//     by key is ignored; × removes;
//   * choosing a user preset adds its tags (ids -> current names; an id that
//     is no longer a tag is skipped) and they can be removed again;
//   * every create path carries the names: POST /api/tasks (JSON array), the
//     single-shot multipart upload (JSON string field), and the multi-file
//     /api/uploads/init (JSON array);
//   * the preset dialog shows the preset's tags and saves them as IDS,
//     creating a missing tag with POST /api/tags first — and on 409 (created
//     meanwhile elsewhere) it re-reads the list and uses the existing id;
//   * at 375px, the field with a 64-character chip and its suggestions open
//     does not make the page scroll sideways.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

import { launch, STATIC_DIR, openFromHeaderMenu } from "../harness.mjs";

export const name = "tags-field";

const CT = {
  ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};

const UPLOAD_ID = "aaaaaaaa-5555-0000-0000-000000000001";
const LONG = "Очень длинное имя тега для проверки переноса на узком экране 1234".slice(0, 64);

function taskRow(id, tags = []) {
  return {
    id, source_url: "https://example.com/v", source_title: "New one",
    status: "queued", options: { transcript: true, prompts: [] }, steps: [],
    created_at: "2026-11-22T10:00:00.000000", updated_at: "2026-11-22T10:00:00.000000",
    progress: {}, stats: {}, tags,
  };
}

async function startServer() {
  const requests = [];
  // "Старый" exists on the server but is not in the first list the page reads
  // (as if another tab created it): POST answers 409 and only the refetch
  // shows its id.
  let tags = [
    { id: "t-biz", name: "Бизнес-клуб", count: 3 },
    { id: "t-fam", name: "Семья", count: 1 },
  ];
  const late = { id: "t-old", name: "Старый", count: 0 };
  let lateVisible = false;
  const presets = [
    { source: "system", id: "default", name: "Default", editable: false,
      options: { transcript: true, prompts: [], delivery: [], tags: [] } },
    { source: "user", id: "p1", name: "Клуб", editable: true,
      options: { transcript: true, prompts: [], delivery: [], tags: ["t-fam", "t-gone"] } },
  ];

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const p = url.pathname;
    if (!p.startsWith("/api/")) {
      const f = p === "/" ? "/index.html" : p.replace("/static/", "/");
      const fp = path.join(STATIC_DIR, f);
      if (!fp.startsWith(STATIC_DIR) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
      const ext = path.extname(fp);
      if (ext === ".woff2" || ext === ".woff" || ext === ".png") { res.end(fs.readFileSync(fp)); return; }
      res.setHeader("Content-Type", CT[ext] || "text/plain");
      res.end(fs.readFileSync(fp).toString().replaceAll("__VTS_VERSION__", "verify"));
      return;
    }
    if (p === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
      res.write(": connected\n\n");
      return;
    }
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks);
      const text = raw.toString("utf8");
      let body = null;
      if ((req.headers["content-type"] || "").includes("application/json")) {
        try { body = JSON.parse(text || "null"); } catch { body = null; }
      }
      requests.push({ method: req.method, path: p, body, text, size: raw.length });
      const json = (status, value) => {
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(value));
      };
      if (p === "/api/tags" && req.method === "GET") {
        return json(200, lateVisible ? [...tags, late] : tags);
      }
      if (p === "/api/tags" && req.method === "POST") {
        const nm = body && body.name;
        if (nm === late.name) { lateVisible = true; return json(409, { detail: "exists" }); }
        const made = { id: `t-new-${tags.length}`, name: nm, count: 0 };
        tags = [...tags, made];
        return json(200, { id: made.id, name: made.name });
      }
      if (p === "/api/tasks" && req.method === "POST") return json(200, taskRow("bbbbbbbb-5555-0000-0000-000000000001"));
      if (p === "/api/tasks/upload" && req.method === "POST") return json(200, taskRow("bbbbbbbb-5555-0000-0000-000000000002"));
      if (p === "/api/uploads/init" && req.method === "POST") return json(200, { upload_id: UPLOAD_ID, chunk_size: 8388608 });
      if (p === `/api/uploads/${UPLOAD_ID}` && req.method === "PATCH") return json(200, { received: raw.length });
      if (p === `/api/uploads/${UPLOAD_ID}/finalize`) return json(200, taskRow(UPLOAD_ID));
      if (req.method !== "GET") return json(200, { status: "ok" });
      const table = {
        "/api/version": { version: "verify" },
        "/api/me": { requested_by: "tester", acting_as: "tester", is_admin: false },
        "/api/push/config": { enabled: false },
        "/api/status-config": { status_flags: {}, tasks_page_size: 20 },
        "/api/tasks": [],
        "/api/prompts": [],
        "/api/presets": presets,
        "/api/me/default_preset": { source: "system", id: "default" },
      };
      return json(200, p in table ? table[p] : {});
    });
  });
  await new Promise((r) => server.listen(0, r));
  return { server, baseUrl: `http://127.0.0.1:${server.address().port}`, requests };
}

const chipNames = (page, scope) => page.$$eval(`${scope} .tag-chip`, (els) =>
  els.map((el) => ({ name: el.querySelector(".tag-chip-label")?.textContent || "", isNew: el.classList.contains("tag-chip--new") })));

async function waitChips(page, scope, want) {
  return page.waitForFunction(([s, w]) => {
    const got = [...document.querySelectorAll(`${s} .tag-chip .tag-chip-label`)].map((e) => e.textContent);
    return JSON.stringify(got) === JSON.stringify(w);
  }, [scope, want], { timeout: 4000 }).then(() => true).catch(() => false);
}

async function waitRequest(requests, pred, timeout = 4000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const hit = requests.find(pred);
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 25));
  }
  return null;
}

export async function run() {
  const failures = [];
  const { server, baseUrl, requests } = await startServer();
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      // The 409 below is provoked on purpose; Chromium logs every non-2xx load.
      if (m.type() === "error" && !m.text().includes("EventSource") && !m.text().includes("409 (Conflict)")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });

    const F = "#task-tags-field";
    const input = `${F} .tag-field-input`;
    const present = await page.waitForSelector(input, { timeout: 8000 }).then(() => true).catch(() => false);
    if (!present) {
      failures.push("the new-task form has no tag field (#task-tags-field .tag-field-input) — nothing below proves anything");
      return failures;
    }

    // ---- suggestions by key ----
    await page.click(input);
    await page.type(input, "биз");
    const suggested = await page.waitForFunction((s) => {
      const items = [...document.querySelectorAll(`${s} .tag-suggestion`)].filter((e) => e.offsetParent);
      return items.length === 1 && items[0].textContent.includes("Бизнес-клуб");
    }, F, { timeout: 4000 }).then(() => true).catch(() => false);
    if (!suggested) {
      const got = await page.$$eval(`${F} .tag-suggestion`, (els) => els.map((e) => e.textContent));
      failures.push(`typing "биз" should suggest exactly "Бизнес-клуб"; suggestions: ${JSON.stringify(got)}`);
    }
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    if (!(await waitChips(page, F, ["Бизнес-клуб"]))) {
      failures.push(`ArrowDown + Enter should add the suggestion; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }
    if ((await page.inputValue(input)) !== "") failures.push("the input should be empty after adding a tag");

    // ---- a new name ----
    await page.type(input, "Новый тег");
    await page.keyboard.press("Enter");
    if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег"]))) {
      failures.push(`Enter should add a typed name; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }
    const chips1 = await chipNames(page, F);
    const fresh = chips1.find((c) => c.name === "Новый тег");
    const known = chips1.find((c) => c.name === "Бизнес-клуб");
    if (!fresh || !fresh.isNew) failures.push("a name that is not an existing tag must carry tag-chip--new");
    if (!known || known.isNew) failures.push("an existing tag must NOT carry tag-chip--new");

    // ---- duplicate by key is ignored; comma adds; click on a suggestion adds ----
    await page.type(input, "  бизнес-КЛУБ ");
    await page.keyboard.press("Enter");
    await page.type(input, "Третий,");
    if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег", "Третий"]))) {
      failures.push(`a duplicate by key must be ignored and a comma must add; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }
    // Enter inside the field must not submit the form.
    if (requests.some((r) => r.method === "POST" && r.path === "/api/tasks")) {
      failures.push("Enter in the tag field submitted the new-task form");
    }
    await page.type(input, "сем");
    const semOk = await page.waitForSelector(`${F} .tag-suggestion >> text=Семья`, { timeout: 3000 }).then(() => true).catch(() => false);
    if (semOk) {
      await page.click(`${F} .tag-suggestion >> text=Семья`);
      if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег", "Третий", "Семья"]))) {
        failures.push(`clicking a suggestion should add it; chips: ${JSON.stringify(await chipNames(page, F))}`);
      }
    } else {
      failures.push('typing "сем" did not suggest "Семья"');
    }
    // × removes
    const removeChip = async (scope, nm) => {
      await page.evaluate(([s, n]) => {
        const chip = [...document.querySelectorAll(`${s} .tag-chip`)].find((c) => c.querySelector(".tag-chip-label")?.textContent === n);
        chip?.querySelector(".tag-chip-remove")?.setAttribute("data-test-target", "1");
      }, [scope, nm]);
      await page.click(`${scope} [data-test-target="1"]`, { timeout: 3000 }).catch(() => {});
    };
    await removeChip(F, "Семья");
    await removeChip(F, "Третий");
    if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег"]))) {
      failures.push(`× should remove a chip; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }

    // ---- a user preset adds its tags (t-gone is skipped) ----
    await page.click("#preset-pill");
    await page.click("#preset-menu .pick-row >> text=Клуб", { timeout: 3000 }).catch(() => {});
    if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег", "Семья"]))) {
      failures.push(`choosing the user preset should add its tag "Семья" (and skip a gone id); chips: ${JSON.stringify(await chipNames(page, F))}`);
    }
    await removeChip(F, "Семья");
    if (!(await waitChips(page, F, ["Бизнес-клуб", "Новый тег"]))) {
      failures.push(`a preset's tag should be removable; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }

    // ---- URL submit carries tags ----
    await page.fill("#url", "https://example.com/v");
    await page.click("#submit-btn");
    const created = await waitRequest(requests, (r) => r.method === "POST" && r.path === "/api/tasks");
    if (!created) failures.push("submitting the form sent no POST /api/tasks");
    else if (JSON.stringify(created.body && created.body.tags) !== JSON.stringify(["Бизнес-клуб", "Новый тег"])) {
      failures.push(`POST /api/tasks tags = ${JSON.stringify(created.body && created.body.tags)}, expected ["Бизнес-клуб","Новый тег"]`);
    }
    // After a create the field starts over from the selected preset's tags.
    if (!(await waitChips(page, F, ["Семья"]))) {
      failures.push(`after a create the field should hold only the selected preset's tags; chips: ${JSON.stringify(await chipNames(page, F))}`);
    }

    // ---- single-shot upload: JSON string form field ----
    await page.evaluate(() => {
      const radio = document.getElementById("source-type-file");
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.setInputFiles("#file-input", [{ name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(500, 1) }]);
    await page.click("#submit-btn", { force: true });
    const single = await waitRequest(requests, (r) => r.method === "POST" && r.path === "/api/tasks/upload");
    if (!single) failures.push("a single-file submit sent no POST /api/tasks/upload");
    else {
      const m = single.text.match(/name="tags"\r\n\r\n([^\r]*)\r\n/);
      if (!m || m[1] !== JSON.stringify(["Семья"])) {
        failures.push(`the multipart upload's tags field = ${m ? m[1] : "(missing)"}, expected ${JSON.stringify(JSON.stringify(["Семья"]))}`);
      }
    }

    // ---- multi-file init: JSON array ----
    await waitChips(page, F, ["Семья"]);
    await page.setInputFiles("#file-input", [
      { name: "a.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(300, 1) },
      { name: "b.mp3", mimeType: "audio/mpeg", buffer: Buffer.alloc(300, 2) },
    ]);
    await page.type(input, "Пара");
    await page.keyboard.press("Enter");
    await page.click("#submit-btn", { force: true });
    const multi = await waitRequest(requests, (r) => r.method === "POST" && r.path === "/api/uploads/init");
    if (!multi) failures.push("a multi-file submit sent no POST /api/uploads/init");
    else if (JSON.stringify(multi.body && multi.body.tags) !== JSON.stringify(["Семья", "Пара"])) {
      failures.push(`/api/uploads/init tags = ${JSON.stringify(multi.body && multi.body.tags)}, expected ["Семья","Пара"]`);
    }
    await waitRequest(requests, (r) => r.path === `/api/uploads/${UPLOAD_ID}/finalize`);

    // ---- preset dialog: shows ids as names, saves ids, creates missing ----
    await openFromHeaderMenu(page, "#presets-btn");
    const P = "#preset-tags-field";
    // Tags are per user and system presets carry none: no field for them.
    const fieldShown = () => page.evaluate((s) => {
      const el = document.querySelector(s);
      return !!el && el.offsetParent !== null && el.getBoundingClientRect().height > 0;
    }, P);
    await page.click("#presets-list .mgr-item >> text=Default", { timeout: 3000 }).catch(() => {});
    if (await fieldShown()) failures.push("the preset editor must not show the tag field for a system preset");
    await page.click("#presets-list .mgr-item >> text=Клуб", { timeout: 3000 }).catch(() => {});
    if (!(await fieldShown())) failures.push("the preset editor must show the tag field for a user preset");
    if (!(await waitChips(page, P, ["Семья"]))) {
      failures.push(`the preset editor should show the preset's tags by name; chips: ${JSON.stringify(await chipNames(page, P))}`);
    }
    const pin = `${P} .tag-field-input`;
    for (const nm of ["Бизнес-клуб", "Проект X", "Старый"]) {
      await page.click(pin, { timeout: 3000 }).catch(() => {});
      await page.type(pin, nm);
      await page.keyboard.press("Enter");
    }
    await waitChips(page, P, ["Семья", "Бизнес-клуб", "Проект X", "Старый"]);
    const before = requests.length;
    await page.click("#preset-submit-btn");
    const patch = await waitRequest(requests, (r) =>
      requests.indexOf(r) >= before && r.method === "PATCH" && r.path === "/api/presets/p1");
    const posted = requests.slice(before).filter((r) => r.method === "POST" && r.path === "/api/tags").map((r) => r.body && r.body.name);
    if (JSON.stringify(posted) !== JSON.stringify(["Проект X", "Старый"])) {
      failures.push(`saving the preset should POST /api/tags for the two new names only; posted ${JSON.stringify(posted)}`);
    }
    if (!patch) failures.push("saving the preset sent no PATCH /api/presets/p1");
    else {
      const sent = patch.body && patch.body.options && patch.body.options.tags;
      const want = ["t-fam", "t-biz", "t-new-2", "t-old"];
      if (JSON.stringify(sent) !== JSON.stringify(want)) {
        failures.push(`PATCH /api/presets/p1 options.tags = ${JSON.stringify(sent)}, expected ids ${JSON.stringify(want)}`);
      }
    }
    await page.click("#presets-close-btn", { timeout: 2000 }).catch(() => {});

    // ---- mobile: no sideways scroll with a long chip and suggestions open ----
    const mobile = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await mobile.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await mobile.waitForSelector(input, { timeout: 8000 });
    await mobile.click(input);
    await mobile.type(input, LONG);
    await mobile.keyboard.press("Enter");
    await mobile.type(input, "с");
    await mobile.waitForSelector(`${F} .tag-suggestion`, { timeout: 3000 }).catch(() => {});
    const overflow = await mobile.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 0) failures.push(`at 375px the tag field makes the page scroll sideways by ${overflow}px`);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

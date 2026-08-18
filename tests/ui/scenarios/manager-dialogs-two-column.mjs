// All four manager dialogs share ONE shape after redesign v2: a selectable list
// on the left, an editor on the right, and every action belonging to whatever is
// open in that editor. Prompts led, then presets, then delivery.
//
// What this guards is the INVARIANT, not one dialog's markup: a row identifies
// its item and is picked. The moment a row grows an icon button again, an action
// can fire against something the user is not looking at — which is exactly what
// the redesign removed, and it regressed once per dialog while it was being done.
//
// The tokens dialog is deliberately NOT in this set: it has no editor (a token
// cannot be edited, only created and revoked), so its rows legitimately keep a
// Revoke button. It is covered by its own row-shape assertions below instead.
import { startStubServer, launch, openPage, openFromHeaderMenu, settled } from "../harness.mjs";

export const name = "manager-dialogs-two-column";

const ADAPTERS = {
  adapters: [{
    name: "outline",
    config_schema: { type: "object", properties: { base_url: { type: "string" } }, required: ["base_url"] },
    secret_keys: ["api_token"],
    connection_fields: ["base_url", "api_token"],
    option_fields: [],
    supports_check: true,
  }],
  incompatible: {},
  variants: [{ value: "summary", label: "delivery.variant.summary" }],
};
const CREDENTIALS = [{
  id: "c1", name: "outline-main", adapter: "outline",
  config: { base_url: "https://outline.example" }, secrets: { api_token: { set: true } },
  adapter_available: true, used_by: 1,
}];
const TARGETS = [{
  id: "t1", name: "meetings", adapter: "outline", credential_id: "c1",
  config: {}, adapter_available: true,
}];

// Each entry: the dialog, how to reach its list, and what the editor's
// delete button is called once a row is picked.
// `pick` names the row to open: it must be a USER-owned item, because a system
// prompt/preset is deliberately not deletable and its Delete button stays hidden
// by design — picking one would make this assert the wrong thing.
const MANAGERS = [
  { label: "prompts",  open: "#prompts-btn",  list: "#prompts-list",  del: "#prompt-delete-btn",  pick: "Memo" },
  { label: "presets",  open: "#presets-btn",  list: "#presets-list",  del: "#preset-delete-btn",  pick: "Standard" },
  { label: "delivery", open: "#delivery-btn", list: "#delivery-credentials-list", del: "#delivery-credential-delete", pick: "outline-main" },
];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/prompts": [
      { source: "system", id: "summary", name: "Summary", editable: false, system_prompt: "Summarize." },
      { source: "user", id: "u1", name: "Memo", editable: true, system_prompt: "Take notes." },
    ],
    "/api/presets": [
      { source: "user", id: "p1", name: "Standard", editable: true,
        options: { language: "ru", transcript: true, prompts: [{ source: "user", id: "u1" }] } },
    ],
    "/api/me/default_preset": { source: "user", id: "p1" },
    "/api/delivery-adapters": ADAPTERS,
    "/api/delivery-credentials": CREDENTIALS,
    "/api/delivery-targets": TARGETS,
    "/api/me/tokens": [
      { id: "k1", name: "obs-laptop", prefix: "vts_a1b2",
        created_at: "2026-08-10T10:00:00Z", last_used_at: "2026-08-18T08:00:00Z" },
    ],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl, { width: 1150, height: 800 });

    for (const m of MANAGERS) {
      await openFromHeaderMenu(page, m.open);
      // The dialog is populated when its list is on screen; waiting for the
      // list itself states that precondition instead of assuming a duration.
      await page.waitForSelector(`${m.list} .mgr-item`, { state: "attached" }).catch(() => {});
      await settled(page);

      const shape = await page.evaluate((sel) => {
        const list = document.querySelector(sel);
        if (!list) return null;
        const rows = [...list.querySelectorAll(".mgr-item")];
        return {
          twoColumn: !!list.closest(".mgr-columns"),
          rows: rows.length,
          // The regression: a row that carries its own actions again.
          rowButtons: list.querySelectorAll(".mgr-item button, .prompts-actions").length,
          // A row must be pickable, i.e. a real control.
          allButtons: rows.every((r) => r.tagName === "BUTTON"),
          named: rows.every((r) => (r.querySelector(".mgr-item-name")?.textContent || "").trim().length > 0),
        };
      }, m.list);

      if (!shape) {
        failures.push(`${m.label}: list ${m.list} not found`);
        continue;
      }
      if (!shape.twoColumn) failures.push(`${m.label}: list is not inside .mgr-columns (two-column layout)`);
      if (!shape.rows) failures.push(`${m.label}: no .mgr-item rows rendered`);
      if (shape.rowButtons) failures.push(`${m.label}: rows carry ${shape.rowButtons} action button(s) — actions belong to the editor`);
      if (!shape.allButtons) failures.push(`${m.label}: some rows are not <button>, so they cannot be picked`);
      if (!shape.named) failures.push(`${m.label}: a row has no .mgr-item-name`);

      // Picking a row must open it AND mark it, so the list says what the
      // editor is showing. Without the mark the two panes look unrelated.
      if (shape.rows) {
        const clicked = await page.evaluate(([sel, want]) => {
          const row = [...document.querySelectorAll(`${sel} .mgr-item`)]
            .find((r) => r.textContent.includes(want));
          if (!row) return false;
          row.click();
          return true;
        }, [m.list, m.pick]);
        if (!clicked) {
          failures.push(`${m.label}: no row named ${JSON.stringify(m.pick)} to pick`);
          await page.keyboard.press("Escape");
          await page.waitForTimeout(250);
          continue;
        }
        // Wait for the POSTCONDITION, not for layout to stop moving. `settled()`
        // resolves as soon as two frames agree, which on a page that has not
        // re-rendered YET is immediately — so under parallel load this read the
        // list before the pick had been applied and reported "0 active rows".
        // (Seen once in a full parallel run; the scenario passed in isolation
        // every time, which is the signature of exactly this race.)
        // Bounded and non-fatal: if the row genuinely never activates, the
        // assertions below still run and report it properly.
        await page
          .waitForFunction(
            (sel) => document.querySelectorAll(`${sel} .mgr-item.active`).length === 1,
            m.list,
            { timeout: 5000 },
          )
          .catch(() => {});
        await settled(page);
        const picked = await page.evaluate(([sel, delSel]) => ({
          active: document.querySelectorAll(`${sel} .mgr-item.active`).length,
          deleteShown: (() => {
            const b = document.querySelector(delSel);
            return !!b && !b.classList.contains("hidden");
          })(),
        }), [m.list, m.del]);
        if (picked.active !== 1) {
          failures.push(`${m.label}: expected exactly 1 active row after picking, got ${picked.active}`);
        }
        if (!picked.deleteShown) {
          failures.push(`${m.label}: the editor's delete button (${m.del}) stayed hidden after picking a row`);
        }
      }

      await page.keyboard.press("Escape");
      await settled(page);
    }

    // ---- Tokens: no editor, so rows keep Revoke — but the row shape is the
    // designed one (name headline, "prefix… · last used" underneath).
    await openFromHeaderMenu(page, "#tokens-btn");
    await page.waitForSelector(".tokens-row", { state: "attached" }).catch(() => {});
    await settled(page);
    const tok = await page.evaluate(() => {
      const row = document.querySelector(".tokens-row");
      if (!row) return null;
      const name = row.querySelector(".tokens-name");
      const sub = row.querySelector(".tokens-sub");
      const revoke = row.querySelector(".tokens-revoke-btn");
      if (!name || !sub || !revoke) return { missing: { name: !name, sub: !sub, revoke: !revoke } };
      const nr = name.getBoundingClientRect();
      const sr = sub.getBoundingClientRect();
      const rr = revoke.getBoundingClientRect();
      return {
        subText: sub.textContent.trim(),
        // Name above its own sub-line, Revoke beside them rather than below.
        stacked: sr.top >= nr.bottom - 1,
        revokeBeside: rr.left > nr.right,
      };
    });
    if (!tok) failures.push("tokens: no .tokens-row rendered");
    else if (tok.missing) failures.push(`tokens: row is missing ${JSON.stringify(tok.missing)}`);
    else {
      if (!tok.stacked) failures.push("tokens: the metadata line is not under the name");
      if (!tok.revokeBeside) failures.push("tokens: Revoke is not beside the token it revokes");
      // The prefix identifies the token; last-use is what says whether revoking breaks something.
      if (!tok.subText.includes("vts_a1b2")) failures.push(`tokens: sub-line lost the prefix: ${JSON.stringify(tok.subText)}`);
      if (!/·/.test(tok.subText)) failures.push(`tokens: sub-line lost its last-used part: ${JSON.stringify(tok.subText)}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

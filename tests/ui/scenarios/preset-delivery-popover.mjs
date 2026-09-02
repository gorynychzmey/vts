// The delivery menu in the PRESET editor must not be trapped inside its pill.
//
// Reported from a screenshot: the "Deliver to" pill grew a horizontal
// scrollbar and spinner arrows, as if the menu were trying to render inside
// the pill's own box.
//
// Cause: a rule written for the PROMPT list,
//     .presets-dialog .prompt-select-field .prompt-select {
//       max-height: 13rem; overflow-y: auto;
//     }
// matches ANY .prompt-select in the dialog — including the delivery field.
// `.prompt-select` is the very element the popover is appended to, and
// overflow:auto makes it a scroll container, so an absolutely-positioned child
// can no longer escape it: the 15rem menu is clipped into a ~9rem pill.
//
// Measured, not eyeballed: a clipped popover reports a scrollWidth wider than
// its parent's clientWidth, and the parent reports overflow != "visible".
import { startStubServer, launch, openPage, dialogOpen, clickReal, openFromHeaderMenu } from "../harness.mjs";

export const name = "preset-delivery-popover";

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/presets": [
      {
        source: "user", id: "p1", name: "Standard", editable: true,
        options: { language: "ru", audio_only: false, transcript: true, prompts: [] },
      },
    ],
    "/api/me/default_preset": { source: "user", id: "p1" },
    // The delivery field only appears when a destination exists.
    "/api/prompts": [
      { source: "user", id: "c32a70c0-fcc7-4d59-82a2-2fe225d85d9d", name: "Мемо", is_system: false },
    ],
    "/api/delivery-adapters": {
      adapters: [
        { name: "webdav", label: "WebDAV", fields: [] },
        { name: "telegram", label: "Telegram", fields: [] },
      ],
      incompatible: {},
      // Variants are per-user (they include the user's own prompts) and are
      // what turns a stored "user:<uuid>" into a name.
      variants: [
        { value: "summary", label: "delivery.variant.summary" },
        { value: "user:c32a70c0-fcc7-4d59-82a2-2fe225d85d9d", label: "Мемо" },
      ],
    },
    "/api/delivery-targets": [
      { id: "d1", name: "Nextcloud", adapter: "webdav", credential_id: "c1",
        config: { default_variant: "user:c32a70c0-fcc7-4d59-82a2-2fe225d85d9d" } },
      { id: "d2", name: "Telegram", adapter: "telegram", credential_id: "c2", config: {} },
    ],
    "/api/delivery-credentials": [
      { id: "c1", name: "nc", adapter: "webdav", config: {} },
      { id: "c2", name: "tg", adapter: "telegram", config: {} },
    ],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page } = await openPage(browser, baseUrl);
    await openFromHeaderMenu(page, "#presets-btn");
    if (!(await dialogOpen(page, "presets-dialog"))) {
      failures.push("presets-dialog did not open");
      return failures;
    }

    // BEFORE picking a preset: the dialog opens on the empty "new preset" form,
    // and the delivery control must already be there. resetPresetForm() drew
    // the prompt list but not this one, so the "Deliver to" label sat above an
    // empty gap until a preset was selected.
    const onEmptyForm = await page.waitForFunction(
      () => {
        const f = document.getElementById("preset-delivery-field");
        return f && !f.hidden && !!f.querySelector(".delivery-pill");
      },
      { timeout: 5000 },
    ).then(() => true).catch(() => false);
    if (!onEmptyForm) {
      const state = await page.evaluate(() => {
        const f = document.getElementById("preset-delivery-field");
        return {
          fieldPresent: !!f,
          hidden: f ? f.hidden : null,
          innerHTML: f ? f.querySelector(".prompt-select")?.innerHTML.length ?? -1 : null,
        };
      });
      failures.push(
        `the delivery control is missing on the new-preset form before any preset is picked: ${JSON.stringify(state)}`,
      );
    }

    // Enter the editor for the user preset.
    await clickReal(page, "#presets-dialog .mgr-item");
    const fieldReady = await page.waitForFunction(
      () => {
        const f = document.getElementById("preset-delivery-field");
        return f && !f.hidden && f.querySelector(".delivery-pill");
      },
      { timeout: 8000 },
    ).then(() => true).catch(() => false);
    if (!fieldReady) {
      failures.push("the delivery field never appeared in the preset editor");
      return failures;
    }

    // The container must not be a scroll container: that is what traps the menu.
    const box = await page.evaluate(() => {
      const sel = document.querySelector("#preset-edit-delivery");
      const cs = getComputedStyle(sel);
      return { overflowY: cs.overflowY, overflowX: cs.overflowX, maxHeight: cs.maxHeight };
    });
    if (box.overflowY !== "visible" || box.overflowX !== "visible") {
      failures.push(
        `#preset-edit-delivery is a scroll container (overflow ${box.overflowX}/${box.overflowY}, max-height ${box.maxHeight}) — an absolutely positioned popover cannot escape it`,
      );
    }

    // And the menu, once open, must be wider than the pill rather than
    // squeezed inside it.
    await clickReal(page, "#preset-edit-delivery .delivery-pill");
    const shape = await page.waitForFunction(
      () => {
        const pop = document.querySelector("#preset-edit-delivery .prompt-select-popover");
        if (!pop || pop.hidden) return null;
        const parent = document.querySelector("#preset-edit-delivery");
        // Overflow INSIDE the menu, not of the pill: the popover is
        // absolutely positioned, so it is legitimately wider than its
        // parent and the parent's scrollWidth always exceeds clientWidth.
        // Measuring that was my own false positive.
        const rows = [...pop.children].map((c) => ({
          cls: (c.className || "").split(" ")[0],
          over: c.scrollWidth - c.clientWidth,
        }));
        return {
          popWidth: Math.round(pop.getBoundingClientRect().width),
          parentWidth: Math.round(parent.getBoundingClientRect().width),
          popOverflow: pop.scrollWidth - pop.clientWidth,
          worstRow: rows.sort((a, b) => b.over - a.over)[0] || null,
        };
      },
      { timeout: 8000 },
    ).then((h) => h.jsonValue()).catch(() => null);

    if (!shape) {
      failures.push("the delivery popover did not open in the preset editor");
    } else {
      // A row wider than the menu is what draws the scrollbar and the
      // spinner arrows in the reported screenshot.
      if (shape.popOverflow > 1) {
        failures.push(
          `the menu scrolls horizontally (scrollWidth exceeds clientWidth by ${shape.popOverflow}px); widest child: ${JSON.stringify(shape.worstRow)}`,
        );
      }
      // 15rem at the default root size; the pill itself is far narrower.
      if (shape.popWidth < 200) {
        failures.push(
          `the popover is ${shape.popWidth}px wide (>=200 expected) — it is being squeezed into the ${shape.parentWidth}px pill`,
        );
      }
    }

    // The checkbox must be a checkbox, not a stretched text field.
    // `.prompt-form input { width: 100% }` caught it and blew it up to 243px
    // in a 259px row, so it took a line of its own above its own label.
    const row = await page.evaluate(() => {
      const r = document.querySelector("#preset-edit-delivery .delivery-row");
      const cb = r.querySelector("input");
      const name = r.querySelector(".prompt-name");
      const top = (el) => Math.round(el.getBoundingClientRect().top);
      return {
        checkboxWidth: Math.round(cb.getBoundingClientRect().width),
        sameLine: Math.abs(top(cb) - top(name)) <= 6,
      };
    });
    if (row.checkboxWidth > 40) {
      failures.push(
        `the checkbox is ${row.checkboxWidth}px wide — a text-field rule is stretching it`,
      );
    }
    if (!row.sameLine) {
      failures.push("the checkbox is not on the same line as the destination name");
    }

    // The variant must read as a NAME. Variants ship with the adapters, and
    // loadDeliveryAdapters used to run only when the delivery MANAGER was
    // opened — so everywhere else the menu printed the stored value verbatim:
    // "user:<uuid>" for a prompt, and even a fixed variant untranslated.
    const labels = await page.$$eval(
      "#preset-edit-delivery .delivery-row-variant",
      (els) => els.map((e) => e.textContent.trim()),
    );
    const raw = labels.filter((l) => /^user:[0-9a-f-]{36}$/i.test(l));
    if (raw.length) {
      failures.push(`the variant shows a raw id instead of a name: ${JSON.stringify(raw)}`);
    }
    if (!labels.includes("Мемо")) {
      failures.push(`expected the prompt's name among the variants, got ${JSON.stringify(labels)}`);
    }

    // And the menu must float over the dialog, not extend it: inside a
    // scrollable dialog a downward popover grows the content and scrolls the
    // form instead. It flips upwards when there is no room below.
    const fit = await page.evaluate(() => {
      const d = document.getElementById("presets-dialog");
      const pop = document.querySelector("#preset-edit-delivery .prompt-select-popover");
      const dr = d.getBoundingClientRect();
      const pr = pop.getBoundingClientRect();
      return {
        inside: pr.bottom <= dr.bottom + 1 && pr.top >= dr.top - 1,
        overflowsBy: Math.round(pr.bottom - dr.bottom),
        dialogScrolls: d.scrollHeight > d.clientHeight + 1,
      };
    });
    if (!fit.inside) {
      failures.push(
        `the menu extends past the dialog by ${fit.overflowsBy}px instead of flipping upwards`,
      );
    }
    if (fit.dialogScrolls) {
      failures.push("opening the menu made the dialog scrollable — it should float over it");
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

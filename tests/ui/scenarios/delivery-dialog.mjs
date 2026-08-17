// Verifies the delivery manager (vts-j2kh): the dialog opens from the header
// menu, and BOTH forms are generated from the adapter's JSON Schema served by
// /api/delivery-adapters — connection fields on the credential form, the rest
// on the destination form. Also asserts the "Deliver to" selector appears in
// the new-task card once a destination exists, and that a secret renders as a
// password input rather than plain text.
import {
  startStubServer, launch, openPage, isVisible, dialogOpen,
  clickReal, screenshot, openFromHeaderMenu,
} from "../harness.mjs";

export const name = "delivery-dialog";

const ADAPTERS = {
  adapters: [
    {
      name: "outline",
      config_schema: {
        type: "object",
        properties: {
          base_url: { type: "string" },
          collection_id: { type: "string" },
        },
        required: ["base_url", "collection_id"],
      },
      secret_keys: ["api_token"],
      connection_fields: ["base_url", "api_token"],
      option_fields: [],
      supports_check: true,
    },
  ],
  incompatible: {},
  // Served by the core, not by any adapter (vts-6fya).
  variants: [
    { value: "raw", label: "delivery.variant.raw" },
    { value: "redacted", label: "delivery.variant.redacted" },
    { value: "summary", label: "delivery.variant.summary" },
    { value: "user:u1", label: "Memo" },
  ],
};

const CREDENTIALS = [
  {
    id: "c0000000-0000-0000-0000-000000000001",
    name: "outline-main",
    adapter: "outline",
    config: { base_url: "https://outline.example/api" },
    secrets: { api_token: { set: true } },
    adapter_available: true,
    used_by: 1,
  },
];

const TARGETS = [
  {
    id: "70000000-0000-0000-0000-000000000001",
    name: "meetings",
    adapter: "outline",
    credential_id: CREDENTIALS[0].id,
    config: { collection_id: "c1" },
    adapter_available: true,
  },
];

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/delivery-adapters": ADAPTERS,
    "/api/delivery-credentials": CREDENTIALS,
    "/api/delivery-targets": TARGETS,
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    if (!(await page.$("#delivery-btn"))) {
      failures.push("no #delivery-btn in header menu");
      return failures;
    }
    await openFromHeaderMenu(page, "#delivery-btn");
    await page.waitForTimeout(300);

    if (!(await dialogOpen(page, "delivery-dialog"))) {
      failures.push("delivery-dialog did not open on #delivery-btn click");
      return failures;
    }
    if (!(await isVisible(page, "#delivery-dialog"))) {
      failures.push("delivery-dialog not visible after open");
    }

    // --- tabs (vts-fepy) ---------------------------------------------------
    // Opens on connections: a destination cannot exist without one.
    const activeFirst = await page.$$eval("[data-delivery-tab].active",
      (els) => els.map((e) => e.dataset.deliveryTab));
    if (JSON.stringify(activeFirst) !== JSON.stringify(["credentials"])) {
      failures.push(`dialog should open on the connections tab, got ${JSON.stringify(activeFirst)}`);
    }
    if (await isVisible(page, "[data-delivery-panel='targets']")) {
      failures.push("the destinations panel must be hidden while its tab is inactive");
    }

    await clickReal(page, "[data-delivery-tab='targets']");
    await page.waitForTimeout(200);
    if (!(await isVisible(page, "[data-delivery-panel='targets']"))) {
      failures.push("destinations panel did not show when its tab was clicked");
    }
    // The CLOSED state is the one that bit before: a bare `display` rule
    // outranks [hidden], so assert the panel is really not visible.
    if (await isVisible(page, "[data-delivery-panel='credentials']")) {
      failures.push("connections panel must hide when the destinations tab is active");
    }
    await clickReal(page, "[data-delivery-tab='credentials']");
    await page.waitForTimeout(200);
    if (!(await isVisible(page, "[data-delivery-panel='credentials']"))) {
      failures.push("connections panel did not come back");
    }

    // Existing rows render in both lists. Redesign v2 made them SELECTABLE
    // .mgr-item buttons (list left, editor right, like the prompts and presets
    // managers); picking a row is what opens it for editing.
    const credRows = await page.$$eval("#delivery-credentials-list .mgr-item", (e) => e.length);
    if (credRows !== 1) failures.push(`expected 1 connection row, got ${credRows}`);
    const targetRows = await page.$$eval("#delivery-targets-list .mgr-item", (e) => e.length);
    if (targetRows !== 1) failures.push(`expected 1 destination row, got ${targetRows}`);

    // The rows carry no actions of their own — delete lives in the editor and
    // acts on whatever is open there.
    const rowBtns = await page.$$eval(
      "#delivery-credentials-list .mgr-item button, #delivery-credentials-list .prompts-actions",
      (e) => e.length,
    );
    if (rowBtns !== 0) failures.push(`connection rows must carry no action buttons, got ${rowBtns}`);

    // --- schema-driven split: the whole point of the feature ---------------
    // base_url is a CONNECTION field, so it belongs to the credential form.
    const credFields = await page.$$eval(
      "#delivery-credential-fields [data-field]",
      (els) => els.map((e) => ({ name: e.dataset.field, type: e.type, tag: e.tagName })),
    );
    const shownLabels = await page.$$eval(
      "#delivery-credential-fields .delivery-field-label", (els) => els.map((e) => e.textContent));
    if (shownLabels.some((l) => /^(base_url|api_token)\b/.test(l))) {
      failures.push(`fields must show human labels, got ${JSON.stringify(shownLabels)}`);
    }
    const credNames = credFields.map((f) => f.name).sort();
    if (JSON.stringify(credNames) !== JSON.stringify(["api_token", "base_url"])) {
      failures.push(`connection form should hold exactly the connection fields, got ${JSON.stringify(credNames)}`);
    }
    const token = credFields.find((f) => f.name === "api_token");
    if (token && token.type !== "password") {
      failures.push(`api_token must render as a password input, got type=${token.type}`);
    }

    // collection_id / default_variant are per-destination, so they belong to
    // the target form — and the enum must become a <select>, not a text box.
    const targetFields = await page.$$eval(
      "#delivery-target-fields [data-field]",
      (els) => els.map((e) => ({ name: e.dataset.field, tag: e.tagName })),
    );
    const targetNames = targetFields.map((f) => f.name).sort();
    if (JSON.stringify(targetNames) !== JSON.stringify(["collection_id"])) {
      failures.push(`destination form should hold the non-connection fields, got ${JSON.stringify(targetNames)}`);
    }
    if (credNames.includes("collection_id") || targetNames.includes("base_url")) {
      failures.push("connection and destination fields leaked across the two forms");
    }

    // The destination form offers the connection to hang off.
    const credOptions = await page.$$eval(
      "#delivery-target-credential option", (els) => els.length);
    if (credOptions !== 1) failures.push(`expected 1 connection option, got ${credOptions}`);

    // --- connection check button (vts-6o37) --------------------------------
    // Hidden on the CREATE form: the check runs server-side against stored
    // secrets, so there must be something stored to check.
    if (await isVisible(page, "#delivery-check-btn")) {
      failures.push("check button must be hidden until a connection is saved");
    }
    // Picking the row opens that connection in the editor.
    await clickReal(page, "#delivery-credentials-list .mgr-item");
    await page.waitForTimeout(250);
    if (!(await isVisible(page, "#delivery-check-btn"))) {
      failures.push("check button should appear when editing a saved connection");
    }

    await page.route("**/api/delivery-credentials/*/check", (route) =>
      route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: false, outcome: "unauthorized", detail: "HTTP 401" }) }));
    await clickReal(page, "#delivery-check-btn");
    await page.waitForTimeout(400);

    const failed = await page.evaluate(() => {
      const b = document.getElementById("delivery-check-btn");
      const m = document.getElementById("delivery-check-message");
      return {
        bad: b.classList.contains("check-bad"),
        msg: m.textContent, shown: !m.hidden,
        // The result must show in the BACKGROUND: tinting only the glyph gave
        // green-on-orange, which was unreadable.
        bg: getComputedStyle(b).backgroundColor,
        // Icon-only, so the row beside the input stays compact.
        text: (b.textContent || "").trim(),
        tooltip: b.getAttribute("data-tooltip") || "",
        besideInput: !!document.querySelector(".delivery-field-with-check #delivery-check-btn"),
      };
    });
    if (!failed.bad) failures.push("a failed check must turn the button red");
    if (failed.bg === "rgba(0, 0, 0, 0)" || /^rgb\(2[0-9]{2}, 2[0-9]{2}, 2[0-9]{2}\)$/.test(failed.bg)) {
      failures.push(`the failure state must colour the BACKGROUND, got ${failed.bg}`);
    }
    if (failed.text) failures.push(`check button must be icon-only, got text ${JSON.stringify(failed.text)}`);
    if (!failed.tooltip) failures.push("icon-only button needs a tooltip naming it");
    if (!failed.besideInput) failures.push("check button should sit beside the endpoint input");
    if (!failed.shown) failures.push("a failed check must show a message");
    // The server sends a CODE; the wording is the UI's, so a diagnosis must
    // appear rather than a generic failure.
    if (!/authenticate|token/i.test(failed.msg)) {
      failures.push(`expected a diagnosis for 'unauthorized', got ${JSON.stringify(failed.msg)}`);
    }
    if (!failed.msg.includes("HTTP 401")) {
      failures.push("the adapter's detail should be shown alongside the message");
    }

    // Any edit invalidates the result — a stale verdict describes settings
    // that no longer exist.
    await page.fill("#delivery-credential-name", "edited");
    await page.waitForTimeout(250);
    const afterEdit = await page.evaluate(() => {
      const b = document.getElementById("delivery-check-btn");
      const m = document.getElementById("delivery-check-message");
      return { bad: b.classList.contains("check-bad"), ok: b.classList.contains("check-ok"), shown: !m.hidden };
    });
    if (afterEdit.bad || afterEdit.ok || afterEdit.shown) {
      failures.push(`editing a field must reset the check state, got ${JSON.stringify(afterEdit)}`);
    }
    await page.unroute("**/api/delivery-credentials/*/check");
    resetDeliveryFormForLaterAssertions: {
      await clickReal(page, "#delivery-credential-cancel");
      await page.waitForTimeout(200);
    }

    await screenshot(page, "delivery-dialog");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
    if (await isVisible(page, "#delivery-dialog")) {
      failures.push("delivery-dialog still visible after Escape");
    }

    // --- the selector in the new-task card ---------------------------------
    if (!(await isVisible(page, "#delivery-select-field"))) {
      failures.push("'Deliver to' selector should be visible when a destination exists");
    }
    // --- the variant belongs to the TARGET, not to each delivery (vts-6fya)
    // The picker that used to sit on every row is gone: which artifact a
    // destination sends is configured once, on the target itself. A row shows
    // it read-only so the choice is visible at the point of use.
    await clickReal(page, "#delivery-select .prompt-select-toggle");
    await page.waitForTimeout(150);

    const rowPickers = await page.$$eval(
      "#delivery-select select.delivery-variant", (els) => els.length);
    if (rowPickers !== 0) {
      failures.push(`per-delivery variant picker should be gone, found ${rowPickers}`);
    }
    const shown = await page.$$eval(
      "#delivery-select .delivery-row-variant", (els) => els.map((e) => e.textContent.trim()));
    if (shown.length !== 1) {
      failures.push(`expected the row to show its target's variant, got ${JSON.stringify(shown)}`);
    }

    // The delivery entry carries only the destination now.
    const refs = await page.evaluate(() => {
      const box = document.querySelector('#delivery-select input[type="checkbox"]');
      if (box && !box.checked) box.click();
      return selectedDeliveryRefs(document.getElementById("delivery-select"));
    });
    if (!Array.isArray(refs) || refs.length !== 1) {
      failures.push(`expected one selected delivery ref, got ${JSON.stringify(refs)}`);
    } else if ("variant" in refs[0]) {
      failures.push(`a delivery entry must not carry a variant any more: ${JSON.stringify(refs[0])}`);
    }

    // --- the target form offers the CORE's variant list, prompts included --
    await openFromHeaderMenu(page, "#delivery-btn");
    await page.waitForTimeout(300);
    const variantOptions = await page.$$eval(
      "#delivery-target-variant option", (els) => els.map((o) => o.value));
    if (!variantOptions.includes("raw") || !variantOptions.includes("summary")) {
      failures.push(`target form must offer the fixed variants, got ${JSON.stringify(variantOptions)}`);
    }
    // Served by the core because it depends on the USER's prompts — something
    // no plugin schema could enumerate.
    if (!variantOptions.includes("user:u1")) {
      failures.push(`target form must offer a user prompt as a variant, got ${JSON.stringify(variantOptions)}`);
    }
    // The adapter no longer declares the field, so it must not appear twice.
    const schemaVariant = await page.$$eval(
      "#delivery-target-fields [data-field='default_variant']", (els) => els.length);
    if (schemaVariant !== 0) {
      failures.push("default_variant must not come from the adapter schema any more");
    }
    await page.keyboard.press("Escape");
    await page.waitForTimeout(150);

    // No horizontal overflow (vts-nr4).
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) failures.push(`page scrolls horizontally by ${overflow}px`);

    if (errors.length) failures.push(`JS errors: ${JSON.stringify(errors)}`);
  } finally {
    await browser.close();
    server.close();
  }

  // The CLOSED state matters as much as the happy path: with no destinations
  // configured the selector must stay hidden rather than offer an empty
  // control the user cannot act on.
  const empty = await startStubServer({
    "/api/delivery-adapters": { adapters: [], incompatible: {} },
    "/api/delivery-credentials": [],
    "/api/delivery-targets": [],
  });
  const browser2 = await launch();
  try {
    const { page, errors } = await openPage(browser2, empty.baseUrl);
    await page.waitForTimeout(300);
    if (await isVisible(page, "#delivery-select-field")) {
      failures.push("'Deliver to' selector must stay hidden when no destination exists");
    }
    await openFromHeaderMenu(page, "#delivery-btn");
    await page.waitForTimeout(300);
    // With no plugins installed the dialog explains why instead of showing
    // two forms that cannot be filled in.
    if (!(await isVisible(page, "#delivery-no-adapters"))) {
      failures.push("expected the 'no plugins installed' hint with zero adapters");
    }
    if (errors.length) failures.push(`JS errors (empty state): ${JSON.stringify(errors)}`);
  } finally {
    await browser2.close();
    empty.server.close();
  }
  return failures;
}

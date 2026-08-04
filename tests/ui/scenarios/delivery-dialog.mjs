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
          default_variant: { type: "string", enum: ["raw", "redacted", "summary"] },
        },
        required: ["base_url", "collection_id"],
      },
      secret_keys: ["api_token"],
      connection_fields: ["base_url", "api_token"],
    },
  ],
  incompatible: {},
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

    // Existing rows render in both lists.
    const credRows = await page.$$eval("#delivery-credentials-list .prompts-row", (e) => e.length);
    if (credRows !== 1) failures.push(`expected 1 connection row, got ${credRows}`);
    const targetRows = await page.$$eval("#delivery-targets-list .prompts-row", (e) => e.length);
    if (targetRows !== 1) failures.push(`expected 1 destination row, got ${targetRows}`);

    // --- schema-driven split: the whole point of the feature ---------------
    // base_url is a CONNECTION field, so it belongs to the credential form.
    const credFields = await page.$$eval(
      "#delivery-credential-fields [data-field]",
      (els) => els.map((e) => ({ name: e.dataset.field, type: e.type, tag: e.tagName })),
    );
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
    if (JSON.stringify(targetNames) !== JSON.stringify(["collection_id", "default_variant"])) {
      failures.push(`destination form should hold the non-connection fields, got ${JSON.stringify(targetNames)}`);
    }
    const variant = targetFields.find((f) => f.name === "default_variant");
    if (variant && variant.tag !== "SELECT") {
      failures.push(`an enum field must render as a <select>, got <${variant.tag}>`);
    }
    if (credNames.includes("collection_id") || targetNames.includes("base_url")) {
      failures.push("connection and destination fields leaked across the two forms");
    }

    // The destination form offers the connection to hang off.
    const credOptions = await page.$$eval(
      "#delivery-target-credential option", (els) => els.length);
    if (credOptions !== 1) failures.push(`expected 1 connection option, got ${credOptions}`);

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
    // Each row carries its OWN variant picker: two destinations on one server
    // may legitimately receive different artifacts (vts-929).
    await clickReal(page, "#delivery-select .prompt-select-toggle");
    await page.waitForTimeout(150);
    const variantPickers = await page.$$eval(
      "#delivery-select .delivery-variant", (els) => els.length);
    if (variantPickers !== 1) {
      failures.push(`expected a per-destination variant picker, got ${variantPickers}`);
    }

    // --- a user prompt's result is offerable as a variant (vts-as1i) ------
    // The default stub carries one user prompt ("Memo", id u1). It must be
    // listed, and DISABLED until that prompt is selected: the server rejects
    // delivering a prompt that will not run, so offering it as selectable
    // would be a trap.
    const memoBefore = await page.$$eval(
      "#delivery-select .delivery-variant option",
      (els) => els.filter((o) => o.value === "user:u1")
                  .map((o) => ({ text: o.textContent, disabled: o.disabled })),
    );
    if (memoBefore.length !== 1) {
      failures.push(`expected the user prompt as a variant option, got ${JSON.stringify(memoBefore)}`);
    } else if (!memoBefore[0].disabled) {
      failures.push("a prompt that is not selected must not be selectable as a variant");
    }

    // Selecting the prompt enables it.
    await clickReal(page, "#prompt-select .prompt-select-toggle");
    await page.waitForTimeout(150);
    const promptBox = await page.$('#prompt-select input[data-source="user"][data-id="u1"]');
    if (!promptBox) {
      failures.push("no checkbox for the user prompt in #prompt-select");
    } else {
      await promptBox.click();
      await page.waitForTimeout(200);
      const memoAfter = await page.$$eval(
        "#delivery-select .delivery-variant option",
        (els) => els.filter((o) => o.value === "user:u1").map((o) => o.disabled),
      );
      if (memoAfter.length !== 1 || memoAfter[0] !== false) {
        failures.push(`selecting the prompt should enable its variant option, got ${JSON.stringify(memoAfter)}`);
      }
    }

    // system:summary is deliberately NOT offered as a ref: it is already
    // reachable as the plain "summary" option.
    const dupSummary = await page.$$eval(
      "#delivery-select .delivery-variant option",
      (els) => els.filter((o) => o.value === "system:summary").length,
    );
    if (dupSummary !== 0) {
      failures.push("system:summary must not be duplicated as a prompt ref");
    }

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

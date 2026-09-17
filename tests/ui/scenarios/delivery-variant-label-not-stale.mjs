// A failed reload of /api/delivery-adapters must not leave the PREVIOUS user's
// prompt names on screen (vts-n7fm).
//
// `variants` is personal data: the list carries this user's own prompts, which
// is why 5d98323 added loadDeliveryAdapters() to refreshAll() — the path an
// admin takes when switching to another user. Its catch cleared `adapters` and
// `incompatible` and left `variants` untouched, so a request that fails during
// the switch (network, 5xx, timeout) keeps the names from the account before it.
//
// Asserted where a person reads it: the "what this destination will send" label
// on the delivery row in the composer. Without the fix that label still says
// "Черновик Алисы" after the switch; with it, the raw stored value shows
// instead, which is honest about not knowing the name.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "delivery-variant-label-not-stale";

// The name that must not survive the switch. Deliberately not an i18n key:
// only a user's own prompt renders its label verbatim.
const FOREIGN_PROMPT_LABEL = "Черновик Алисы";

const ADAPTERS = {
  adapters: [
    {
      name: "outline",
      config_schema: { type: "object", properties: { collection_id: { type: "string" } } },
      secret_keys: [],
      connection_fields: [],
      option_fields: [],
      supports_check: false,
    },
  ],
  incompatible: {},
  variants: [
    { value: "summary", label: "delivery.variant.summary" },
    { value: "user:u1", label: FOREIGN_PROMPT_LABEL },
  ],
};

const CREDENTIALS = [
  {
    id: "c0000000-0000-0000-0000-000000000001",
    name: "outline-main",
    adapter: "outline",
    config: {},
    secrets: {},
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
    // The destination sends a user prompt's result, so its label comes from
    // `variants` rather than from an i18n key.
    config: { default_variant: "user:u1" },
    adapter_available: true,
  },
];

const variantText = (page) =>
  page.evaluate(() => {
    const el = document.querySelector(".delivery-row-variant");
    return el ? el.textContent : null;
  });

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/delivery-adapters": ADAPTERS,
    "/api/delivery-credentials": CREDENTIALS,
    "/api/delivery-targets": TARGETS,
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // Setup guard: without this the test could pass on a page that never
    // rendered the label at all.
    const before = await variantText(page);
    if (before !== FOREIGN_PROMPT_LABEL) {
      failures.push(
        `setup: the delivery row shows ${JSON.stringify(before)} instead of ` +
        `${JSON.stringify(FOREIGN_PROMPT_LABEL)} — the rest of this scenario ` +
        `proves nothing`
      );
      return failures;
    }

    // The user switch: refreshAll() reloads the personal data, and the adapters
    // request is the one that fails. The destinations still load, so the row is
    // re-rendered — with whatever `variants` holds at that moment.
    await page.route("**/api/delivery-adapters", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "boom" }),
      }));
    await page.evaluate(() => refreshAll());
    await page.waitForFunction(
      () => !!document.querySelector(".delivery-row-variant"),
      null, { timeout: 5000 },
    );

    const after = await variantText(page);
    if (after === FOREIGN_PROMPT_LABEL) {
      failures.push(
        `the delivery row still reads ${JSON.stringify(after)} after the ` +
        `adapters request failed — that is the previous account's prompt name`
      );
    } else if (after !== "user:u1") {
      failures.push(
        `expected the raw stored value "user:u1" once the label is unknown, ` +
        `got ${JSON.stringify(after)}`
      );
    }

    // Two logs are provoked on purpose: the browser's own note about the 500,
    // and the handler's. Anything else is a real defect.
    const expected = [
      "Failed to load delivery adapters",   // app.js catch
      "the server responded with a status of 500",  // Chromium, for the route
    ];
    const unexpected = errors.filter((e) => !expected.some((x) => e.includes(x)));
    if (unexpected.length) failures.push("JS errors: " + JSON.stringify(unexpected));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

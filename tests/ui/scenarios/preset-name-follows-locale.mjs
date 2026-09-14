// The preset pill must follow the UI language (reported 2026-09-14).
//
// Screenshot: the interface is German, the pill still reads "По умолчанию".
//
// applyI18nToPage() rewrites [data-i18n] nodes only. The pill's label is set
// from JS — updatePresetSaveBtn() assigns textContent from presetLabel(), which
// translates a SYSTEM preset through `preset.system.<id>`. That key exists in
// all three locales, but nothing re-runs it on a locale change, so the label
// keeps whatever language it was first painted in.
//
// repaintJsBuiltLabels() already exists for exactly this class of widget and
// covers the prompt pills, the delivery pills, the filter segments and the task
// cards — the preset pill was simply missing from it.
import { startStubServer, launch, openPage, clickReal, settled } from "../harness.mjs";

export const name = "preset-name-follows-locale";

// The name the SERVER sends is deliberately English: a system preset carries a
// stable identifier, and the visible name comes from the locale file. If the
// pill ever showed this string the translation path would be bypassed.
const PRESETS = [
  {
    source: "system", id: "default", name: "Default", editable: false,
    options: { language: "", audio_only: false, transcript: true, prompts: [] },
  },
];

const EXPECTED = { en: "Default", ru: "По умолчанию", de: "Standard" };

const pillText = (page) =>
  page.$eval("#preset-pill-label", (el) => el.textContent.trim()).catch(() => null);

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/presets": PRESETS,
    "/api/me/default_preset": { source: "system", id: "default" },
  });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));

    await page.waitForFunction(
      () => (document.getElementById("preset-pill-label")?.textContent || "").trim().length > 0,
      { timeout: 8000 },
    );

    const started = await pillText(page);
    if (started !== EXPECTED.en) {
      failures.push(`initial pill = "${started}", expected "${EXPECTED.en}"`);
    }

    // The header menu stays open across picks, so it is opened once.
    await clickReal(page, "#header-menu-btn");
    await page.waitForSelector("#locale-toggle-btn", { state: "visible", timeout: 8000 });

    // The toggle cycles en -> ru -> de -> en; check every step, because a
    // repaint that only happens to run once would pass a single transition.
    for (const next of ["ru", "de", "en"]) {
      await clickReal(page, "#locale-toggle-btn");
      await page.waitForFunction((l) => document.documentElement.lang === l, next, { timeout: 8000 });
      await settled(page);

      const label = await pillText(page);
      if (label !== EXPECTED[next]) {
        failures.push(
          `after switching to ${next} the preset pill reads "${label}", expected "${EXPECTED[next]}"`,
        );
      }
      // The server's English name must never leak through in a translated
      // locale — that would mean presetLabel() was bypassed rather than re-run.
      if (next !== "en" && label === "Default") {
        failures.push(`in ${next} the pill shows the server-side name instead of the translation`);
      }
      // The Save button sits next to the pill and is painted by the same
      // function, so it is the cheapest place to notice the repaint being
      // half-applied.
      const save = await page.$eval("#preset-save-label", (el) => el.textContent.trim())
        .catch(() => null);
      // Exact strings, read from the locale files rather than guessed.
      const SAVE = {
        en: "Save as preset",
        ru: "Сохранить как шаблон",
        de: "Als Preset speichern",
      };
      if (save !== SAVE[next]) {
        failures.push(
          `after switching to ${next} the save button reads "${save}", expected "${SAVE[next]}"`,
        );
      }
    }

    // The SAME name in the presets MANAGER. Separate render path
    // (renderPresetsListFromCache), and the dialog can be open across a
    // language change — which is the situation the report came from.
    await clickReal(page, "#presets-btn");
    await page.waitForSelector("#presets-dialog .mgr-item-name", { timeout: 8000 });

    const dialogNames = () => page.$$eval(
      "#presets-dialog .mgr-item-name", (els) => els.map((e) => e.textContent.trim()));

    for (const next of ["ru", "de"]) {
      // Click the toggle directly: the open dialog covers the header menu.
      await page.evaluate(() => document.getElementById("locale-toggle-btn")?.click());
      await page.waitForFunction((l) => document.documentElement.lang === l, next, { timeout: 8000 });
      await settled(page);

      const names = await dialogNames();
      if (!names.includes(EXPECTED[next])) {
        failures.push(
          `in ${next} the presets manager lists ${JSON.stringify(names)}; expected "${EXPECTED[next]}" among them`,
        );
      }
      if (names.includes("Default")) {
        failures.push(`in ${next} the presets manager still shows the server-side "Default"`);
      }
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

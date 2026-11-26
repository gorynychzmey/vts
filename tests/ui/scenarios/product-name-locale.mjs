// The German interface already carries the product's new name, Ohrchiv, in the
// header and the browser tab; the other languages keep their current wording
// until the rename reaches them. The tab title is not a [data-i18n] node, so it
// must follow a live locale switch too, not only a fresh load.
import { startStubServer, launch, settled, clickReal } from "../harness.mjs";

export const name = "product-name-locale";

const EXPECTED = {
  de: { header: "Ohrchiv", title: "Ohrchiv" },
  en: { header: "Video Transcribe & Summarize", title: "vts" },
  ru: { header: "Транскрибация и суммаризация видео", title: "vts" },
};

const read = (page) =>
  page.evaluate(() => ({
    lang: document.documentElement.lang,
    header: document.querySelector(".brand-title")?.textContent?.trim(),
    title: document.title,
  }));

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    for (const [locale, want] of Object.entries(EXPECTED)) {
      const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
      await page.goto(baseUrl, { waitUntil: "networkidle" });
      await page.evaluate((l) => localStorage.setItem("vts_locale", l), locale);
      await page.reload({ waitUntil: "networkidle" });
      await page.waitForFunction((l) => document.documentElement.lang === l, locale, { timeout: 5000 });
      await settled(page);
      const got = await read(page);
      if (got.header !== want.header)
        failures.push(`${locale}: header = "${got.header}", expected "${want.header}"`);
      if (got.title !== want.title)
        failures.push(`${locale}: tab title = "${got.title}", expected "${want.title}"`);
      await page.close();
    }

    // Live switch de -> en: the tab title must drop the German name.
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.evaluate(() => localStorage.setItem("vts_locale", "de"));
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForFunction(() => document.documentElement.lang === "de", null, { timeout: 5000 });
    await settled(page);
    await clickReal(page, "#header-menu-btn");
    await settled(page);
    await clickReal(page, "#locale-toggle-btn");
    await page.waitForFunction(() => document.documentElement.lang === "en", null, { timeout: 5000 });
    await settled(page);
    const got = await read(page);
    if (got.title !== "vts") failures.push(`after de -> en switch: tab title = "${got.title}", expected "vts"`);
    if (got.header !== EXPECTED.en.header)
      failures.push(`after de -> en switch: header = "${got.header}", expected "${EXPECTED.en.header}"`);
    await page.close();
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

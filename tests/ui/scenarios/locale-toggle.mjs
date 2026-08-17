// Redesign v2, stage 2: the UI-language control.
// Until this existed the interface language came only from navigator.languages
// and could not be overridden. Cycles en -> ru -> de -> en, persists the
// choice, and outranks browser detection on the next load.
import { startStubServer, launch, openPage, isVisible, clickReal } from "../harness.mjs";

export const name = "locale-toggle";

const readState = (page) =>
  page.evaluate(() => ({
    lang: document.documentElement.lang,
    stored: localStorage.getItem("vts_locale"),
    endonym: document.getElementById("locale-toggle-label")?.textContent?.trim(),
    // A translated node, to prove the whole page followed and not just the label.
    version: document.querySelector('[data-i18n="header.version"]')?.textContent?.trim(),
    themeLabel: document.getElementById("theme-toggle-label")?.textContent?.trim(),
  }));

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));

    await clickReal(page, "#header-menu-btn");
    await page.waitForTimeout(150);
    if (!(await isVisible(page, "#locale-toggle-btn")))
      failures.push("#locale-toggle-btn not visible in the header menu");

    let st = await readState(page);
    if (st.endonym !== "English") failures.push(`initial endonym = "${st.endonym}", expected "English"`);

    // en -> ru
    await clickReal(page, "#locale-toggle-btn");
    await page.waitForTimeout(400);
    st = await readState(page);
    if (st.lang !== "ru") failures.push(`after 1st click <html lang> = "${st.lang}"`);
    if (st.stored !== "ru") failures.push(`after 1st click stored = "${st.stored}"`);
    if (st.endonym !== "Русский") failures.push(`ru endonym = "${st.endonym}"`);
    if (!/Версия/.test(st.version || "")) failures.push(`page did not translate: version label = "${st.version}"`);
    // The theme label is a [data-i18n] node, so applyI18n rewrites it — it must
    // survive as the translated theme string, not revert to English.
    if (!/Тема/.test(st.themeLabel || ""))
      failures.push(`theme label not re-synced after locale switch: "${st.themeLabel}"`);

    // ru -> de
    await clickReal(page, "#locale-toggle-btn");
    await page.waitForTimeout(400);
    st = await readState(page);
    if (st.lang !== "de") failures.push(`after 2nd click <html lang> = "${st.lang}"`);
    if (st.endonym !== "Deutsch") failures.push(`de endonym = "${st.endonym}"`);

    // de -> en (wraps)
    await clickReal(page, "#locale-toggle-btn");
    await page.waitForTimeout(400);
    st = await readState(page);
    if (st.lang !== "en") failures.push(`after 3rd click <html lang> = "${st.lang}", expected wrap to en`);

    // Persistence: the stored choice must beat browser detection on reload.
    await page.evaluate(() => localStorage.setItem("vts_locale", "de"));
    await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(400);
    st = await readState(page);
    if (st.lang !== "de") failures.push(`stored locale not restored on reload: lang = "${st.lang}"`);
    if (st.endonym !== "Deutsch") failures.push(`endonym after reload = "${st.endonym}"`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Redesign v2, stage 2: the three-state theme control.
// system -> light -> dark -> system, persisted, and applied before first paint.
import { startStubServer, launch, openPage, isVisible, clickReal } from "../harness.mjs";

export const name = "theme-toggle";

const readState = (page) =>
  page.evaluate(() => ({
    attr: document.documentElement.getAttribute("data-theme"),
    stored: localStorage.getItem("vts_theme"),
    label: document.getElementById("theme-toggle-label")?.textContent?.trim(),
    bg: getComputedStyle(document.documentElement).getPropertyValue("--bg").trim(),
    icons: ["system", "light", "dark"].filter(
      (n) => !document.getElementById(`theme-icon-${n}`)?.classList.contains("hidden"),
    ),
    metas: [...document.querySelectorAll('meta[name="theme-color"]')].map((m) => ({
      content: m.getAttribute("content"),
      media: m.getAttribute("media"),
    })),
  }));

async function openMenu(page) {
  await clickReal(page, "#header-menu-btn");
  await page.waitForTimeout(150);
}

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));

    // --- default: system (no attribute, nothing stored) ---
    let st = await readState(page);
    if (st.attr !== null) failures.push(`default should have no data-theme, got "${st.attr}"`);
    if (st.stored !== null) failures.push(`default should store nothing, got "${st.stored}"`);
    if (JSON.stringify(st.icons) !== '["system"]') failures.push(`default icon = ${JSON.stringify(st.icons)}`);
    if (st.metas.length !== 2 || !st.metas.every((m) => m.media))
      failures.push(`system should keep the media-scoped theme-color pair, got ${JSON.stringify(st.metas)}`);

    await openMenu(page);
    if (!(await isVisible(page, "#theme-toggle-btn")))
      failures.push("#theme-toggle-btn not visible in the header menu");

    // --- cycle 1: system -> light ---
    await clickReal(page, "#theme-toggle-btn");
    await page.waitForTimeout(120);
    st = await readState(page);
    if (st.attr !== "light") failures.push(`after 1st click data-theme = "${st.attr}", expected "light"`);
    if (st.stored !== "light") failures.push(`after 1st click stored = "${st.stored}"`);
    if (st.bg !== "#efeae0") failures.push(`light --bg = ${st.bg}`);
    if (JSON.stringify(st.icons) !== '["light"]') failures.push(`light icon = ${JSON.stringify(st.icons)}`);
    if (st.metas.length !== 1 || st.metas[0].media)
      failures.push(`explicit light should pin ONE unscoped theme-color, got ${JSON.stringify(st.metas)}`);

    // --- cycle 2: light -> dark ---
    await clickReal(page, "#theme-toggle-btn");
    await page.waitForTimeout(120);
    st = await readState(page);
    if (st.attr !== "dark") failures.push(`after 2nd click data-theme = "${st.attr}"`);
    if (st.stored !== "dark") failures.push(`after 2nd click stored = "${st.stored}"`);
    if (st.bg !== "#191512") failures.push(`dark --bg = ${st.bg}`);
    if (JSON.stringify(st.icons) !== '["dark"]') failures.push(`dark icon = ${JSON.stringify(st.icons)}`);

    // --- cycle 3: dark -> system (storage cleared, attribute removed) ---
    await clickReal(page, "#theme-toggle-btn");
    await page.waitForTimeout(120);
    st = await readState(page);
    if (st.attr !== null) failures.push(`after 3rd click data-theme = "${st.attr}", expected none`);
    if (st.stored !== null) failures.push(`after 3rd click stored = "${st.stored}", expected cleared`);

    // --- persistence + anti-flash: dark must be on the FIRST painted frame ---
    // Reload THIS page rather than opening a new one: browser.newPage() gets a
    // fresh context with its own empty localStorage, so a new page could never
    // see the stored value and the assertion would fail for the wrong reason.
    await page.evaluate(() => localStorage.setItem("vts_theme", "dark"));
    // Capture data-theme as early as a script can observe it. If the attribute
    // were only set by app.js (loaded at the end of <body>), this reads "none"
    // and the user would see a light flash on every load.
    await page.addInitScript(() => {
      document.addEventListener("readystatechange", () => {
        if (!window.__earlyTheme && document.readyState === "interactive") {
          window.__earlyTheme = document.documentElement.getAttribute("data-theme") || "none";
        }
      });
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    const earliest = await page.evaluate(() => window.__earlyTheme || null);
    if (earliest !== "dark")
      failures.push(`anti-flash: data-theme at interactive = "${earliest}", expected "dark" (light flash on load)`);
    const reloaded = await readState(page);
    if (reloaded.attr !== "dark") failures.push(`stored theme not restored on reload: "${reloaded.attr}"`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

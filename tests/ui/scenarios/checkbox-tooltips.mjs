// Verifies the New Task checkbox tooltips (vts-rgj). The two option pills carry
// a native `title` (the same mechanism the long restart-menu tooltips use — the
// CSS [data-tooltip] bubble is `white-space: nowrap`, so it only fits the short
// icon-button labels, not a two-sentence explanation).
// Asserts: both pills expose a non-empty title; i18n actually substitutes it
// (the RU title differs from the EN default and says "сводк*", per vts-5ti);
// and the audio_only pill keeps its title while hidden on the File source.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "checkbox-tooltips";

const AUDIO_PILL = "#audio-only-pill";
const TRANSCRIPT_PILL = "label.option-pill:has(#transcript)";

async function titleOf(page, sel) {
  return page.evaluate((s) => {
    const el = document.querySelector(s);
    return el ? el.getAttribute("title") || "" : null;
  }, sel);
}

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({});
  const browser = await launch();

  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForSelector(AUDIO_PILL, { timeout: 5000 });

    // --- both pills carry a non-empty, explanatory title ---
    for (const [sel, label] of [[AUDIO_PILL, "audio_only"], [TRANSCRIPT_PILL, "transcript"]]) {
      const title = await titleOf(page, sel);
      if (title === null) {
        failures.push(`${label}: pill not found (${sel})`);
        continue;
      }
      if (!title.trim()) failures.push(`${label}: title is empty — the checkbox stays unexplained`);
      // A bare restatement of the label teaches nothing; require real prose.
      if (title.trim().split(/\s+/).length < 5) {
        failures.push(`${label}: title too short to explain anything ("${title}")`);
      }
    }

    // The transcript tooltip must state the dependency — that is the whole point
    // of the ticket ("без транскрипции нет никакой суммаризации").
    const transcriptTitle = (await titleOf(page, TRANSCRIPT_PILL)) || "";
    if (!/summar/i.test(transcriptTitle)) {
      failures.push(`transcript: title must explain the summary dependency, got "${transcriptTitle}"`);
    }
    // The audio_only tooltip must scope itself to downloading, not the pipeline.
    const audioTitle = (await titleOf(page, AUDIO_PILL)) || "";
    if (!/download/i.test(audioTitle)) {
      failures.push(`audio_only: title must say it only affects downloading, got "${audioTitle}"`);
    }

    // --- i18n substitution actually happens (not just the HTML default) ---
    // The app picks the locale from navigator.languages (app.js detectLocale),
    // so drive it the way a real RU browser would rather than poking internals.
    const enAudio = audioTitle;
    const ruContext = await browser.newContext({
      viewport: { width: 1100, height: 700 },
      locale: "ru-RU",
    });
    const ruPage = await ruContext.newPage();
    await ruPage.goto(baseUrl, { waitUntil: "networkidle" });
    await ruPage.waitForSelector(AUDIO_PILL, { timeout: 5000 });
    const ruAudio = (await titleOf(ruPage, AUDIO_PILL)) || "";
    const ruTranscript = (await titleOf(ruPage, TRANSCRIPT_PILL)) || "";
    await ruContext.close();

    if (ruAudio === enAudio) {
      failures.push("i18n: RU title identical to EN — data-i18n-title not applied to the pill");
    }
    // vts-5ti: RU says "сводка", never "саммари"/"summary".
    for (const [t, label] of [[ruAudio, "audio_only"], [ruTranscript, "transcript"]]) {
      if (/саммари/i.test(t)) failures.push(`${label} RU: uses "саммари" — must be "сводка" (vts-5ti): "${t}"`);
      if (!/сводк/i.test(t)) failures.push(`${label} RU: expected "сводк*" wording, got "${t}"`);
    }

    // --- the hidden audio_only pill keeps its explanation (File source) ---
    await page.click("label:has(#source-type-file)");
    await page.waitForTimeout(80);
    const hiddenTitle = (await titleOf(page, AUDIO_PILL)) || "";
    if (!hiddenTitle.trim()) {
      failures.push("audio_only: title lost when the pill is hidden on the File source");
    }

    // --- unchecking Transcript dims language + prompts, but keeps their values ---
    await page.click("label:has(#source-type-url)");
    await page.selectOption("#language", "ru");
    await page.click("#transcript"); // uncheck
    await page.waitForTimeout(80);

    const off = await page.evaluate(() => ({
      langDimmed: document.getElementById("language-control")?.classList.contains("disabled"),
      langDisabled: document.getElementById("language")?.disabled,
      langValue: document.getElementById("language")?.value,
      promptsDimmed: document.getElementById("prompt-select")?.classList.contains("disabled"),
      langVisible: !!document.getElementById("language-control")?.offsetParent,
    }));
    if (!off.langDimmed) failures.push("transcript off: language control not dimmed");
    if (!off.langDisabled) failures.push("transcript off: language select still interactive");
    if (!off.promptsDimmed) failures.push("transcript off: prompt select not dimmed (pre-existing behavior lost)");
    // Dimmed, NOT hidden — the point is to show the dependency, not hide it.
    if (!off.langVisible) failures.push("transcript off: language control hidden instead of dimmed");
    // Never clear the value: currentFormOptions() reads it and a cleared value
    // would mark a preset dirty, letting a save overwrite it (vts-86k class).
    if (off.langValue !== "ru") failures.push(`transcript off: language value was cleared ("${off.langValue}") — preset-corruption risk`);

    // --- re-checking Transcript restores both ---
    await page.click("#transcript"); // check again
    await page.waitForTimeout(80);
    const on = await page.evaluate(() => ({
      langDimmed: document.getElementById("language-control")?.classList.contains("disabled"),
      langDisabled: document.getElementById("language")?.disabled,
      langValue: document.getElementById("language")?.value,
      promptsDimmed: document.getElementById("prompt-select")?.classList.contains("disabled"),
    }));
    if (on.langDimmed || on.langDisabled) failures.push("transcript on: language still dimmed/disabled");
    if (on.promptsDimmed) failures.push("transcript on: prompts still dimmed");
    if (on.langValue !== "ru") failures.push(`transcript on: language value lost ("${on.langValue}")`);

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }

  return failures;
}

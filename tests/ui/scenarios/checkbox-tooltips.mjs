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

// data-i18n-title renders through the styled bubble (data-tooltip), not the
// native title — the native one never appears on touch.
async function titleOf(page, sel) {
  return page.evaluate((s) => {
    const el = document.querySelector(s);
    return el ? el.getAttribute("data-tooltip") || "" : null;
  }, sel);
}

// Phone widths where .options-row becomes a two-column grid (vts-nr4), so a
// left-column pill is far narrower than its own tooltip.
const NARROW = [320, 360, 412];

export async function run() {
  const failures = [];
  // Delivery data so the DELIVERY PILL actually renders: it is built by JS after
  // the page's applyI18n pass and was the control drawing a native browser
  // tooltip, so a fixture without it makes the checks below assert nothing.
  const { server, baseUrl } = await startStubServer({
    "/api/delivery-adapters": {
      adapters: [{
        name: "outline",
        config_schema: { type: "object", properties: { base_url: { type: "string" } }, required: ["base_url"] },
        secret_keys: [], connection_fields: ["base_url"], option_fields: [], supports_check: false,
      }],
      incompatible: {},
      variants: [{ value: "summary", label: "delivery.variant.summary" }],
    },
    "/api/delivery-credentials": [{
      id: "c1", name: "outline-main", adapter: "outline",
      config: { base_url: "https://outline.example" }, secrets: {},
      adapter_available: true, used_by: 1,
    }],
    "/api/delivery-targets": [{
      id: "t1", name: "meetings", adapter: "outline", credential_id: "c1",
      config: {}, adapter_available: true,
    }],
  });
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

    // --- the bubble looks like a native tooltip and reveals on hover ---
    const style = await page.evaluate((s) => {
      const cs = getComputedStyle(document.querySelector(s), "::after");
      return { bg: cs.backgroundColor, color: cs.color, borderW: cs.borderTopWidth,
               borderC: cs.borderTopColor, whiteSpace: cs.whiteSpace, rest: cs.opacity };
    }, AUDIO_PILL);
    // The intent is "light surface, dark text" like the native tooltip — not one
    // exact white. Asserting a literal rgb() froze the bubble out of the theme:
    // it has to follow --bg-card so it inverts with everything else in dark mode.
    // Check the relationship instead: the surface must be far lighter than the text.
    const lum = (rgb) => {
      const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map(Number);
      const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    if (lum(style.bg) < 0.7) failures.push(`bubble surface should be light, got ${style.bg}`);
    if (lum(style.bg) - lum(style.color) < 0.5)
      failures.push(`bubble should be dark text on a light surface, got ${style.color} on ${style.bg}`);
    if (style.borderW === "0px") failures.push("bubble has no border — should read like the native tooltip");
    if (style.color === "rgb(255, 255, 255)") failures.push("bubble text is white on white");
    // Long tooltips must wrap; a nowrap bubble overflows its container (vts-7rj).
    if (style.whiteSpace === "nowrap") failures.push("bubble is nowrap — long text will overflow and clip");
    if (style.rest !== "0") failures.push(`bubble should be hidden at rest, opacity=${style.rest}`);
    await page.hover(AUDIO_PILL);
    // Tooltips have a ~0.5s show-delay (so a sweeping pointer doesn't flash
    // them); wait past it plus the fade before asserting the revealed state.
    await page.waitForTimeout(800);
    const shown = await page.evaluate((s) => getComputedStyle(document.querySelector(s), "::after").opacity, AUDIO_PILL);
    if (shown !== "1") failures.push(`bubble did not reveal on hover, opacity=${shown}`);
    await page.mouse.move(0, 0);

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

    // --- the bubble must FIT ON SCREEN at phone widths -------------------
    // Reported from the shipped build: on a phone the option tooltip was cut
    // off at the left edge, mid-sentence. Below 40rem .options-row is a
    // two-column grid, so a left-column pill is ~180px while the bubble is up
    // to 256px — and the shared rule right-anchors the bubble to its trigger,
    // which put its left edge at x=-74. `max-width` could not save it: that
    // caps the bubble's WIDTH, not where it starts.
    //
    // Measured on the PAINTED box, not on scrollWidth: a bubble that overflows
    // to the LEFT never adds scrollWidth, so the sideways-scroll probes used
    // elsewhere in this suite are blind to exactly this failure. That blindness
    // is why it reached a release.
    for (const width of NARROW) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(200);
      const clipped = await page.evaluate(() => {
        const vw = document.documentElement.clientWidth;
        const out = [];
        for (const pill of document.querySelectorAll("#task-form .option-pill[data-tooltip]")) {
          const cs = getComputedStyle(pill, "::after");
          const w = parseFloat(cs.width);
          if (!w) continue;
          const r = pill.getBoundingClientRect();
          // Derive the bubble's box from whichever edge it is anchored to.
          const left = cs.left !== "auto" ? r.left + parseFloat(cs.left) : r.right - w;
          if (left < -1 || left + w > vw + 1) {
            out.push({
              pill: (pill.textContent || "").trim().slice(0, 24),
              spans: `${Math.round(left)}..${Math.round(left + w)}`,
              viewport: vw,
            });
          }
        }
        return out;
      });
      for (const c of clipped) {
        failures.push(
          `[${width}px] option tooltip runs off screen: "${c.pill}" spans ${c.spans} of 0..${c.viewport}`
        );
      }
    }
    await page.setViewportSize({ width: 1100, height: 700 });

    // --- ONE tooltip style, everywhere -----------------------------------
    // Three visibly different tooltips shipped: the styled bubble, and — on the
    // delivery pill — the browser's own native tooltip. That pill is built by
    // JS AFTER applyI18n has run over the page, and it set a raw `title` as well
    // as data-i18n-title, so nothing ever converted it. A raw `title` left on
    // any element is the tell, so assert on that rather than on colours.
    const rawTitles = await page.evaluate(() =>
      [...document.querySelectorAll("#task-form [title]")].map(
        (e) => e.tagName + "." + String(e.className).slice(0, 30)
      )
    );
    if (rawTitles.length) {
      failures.push(
        `elements still carry a raw title=, so the browser draws its own tooltip ` +
        `instead of the styled bubble: ${JSON.stringify(rawTitles)}`
      );
    }

    // And the bubbles that DO exist must all look the same.
    const styles = await page.evaluate(() => {
      const pick = ["#preset-pill", "#audio-only-pill", ".delivery-pill"];
      const seen = {};
      for (const sel of pick) {
        const el = document.querySelector(sel);
        // A control with no data-tooltip is the regression itself: its
        // data-i18n-title was never converted, so the browser draws its own
        // tooltip instead of the styled bubble. Record it rather than skip it.
        if (!el) continue;
        if (!el.hasAttribute("data-tooltip")) { seen[sel] = "NO-BUBBLE"; continue; }
        const cs = getComputedStyle(el, "::after");
        seen[sel] = [cs.backgroundColor, cs.color, cs.borderTopWidth, cs.borderTopColor, cs.borderTopLeftRadius].join("|");
      }
      return seen;
    });
    // The delivery pill is the one that regressed, so its absence would make
    // both checks above vacuous. Fail loudly rather than quietly pass.
    if (!styles[".delivery-pill"]) {
      failures.push("the delivery pill did not render — these tooltip checks assert nothing without it");
    } else if (styles[".delivery-pill"] === "NO-BUBBLE") {
      failures.push(
        "the delivery pill has no data-tooltip — its data-i18n-title was not converted, " +
        "so the browser draws its own native tooltip instead of the styled bubble"
      );
    }
    const distinct = new Set(Object.values(styles));
    if (Object.keys(styles).length > 1 && distinct.size > 1) {
      failures.push(`tooltip styles diverge between controls: ${JSON.stringify(styles)}`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }

  return failures;
}

// Diana's feedback (vts-lbgg): "пресет" is opaque to Russian-speaking users.
// The term became "Шаблон", and the selector got a "?" affordance explaining
// what it is.
//
// Three things are asserted, and the third is the one that matters most:
//
//  1. The Russian UI no longer says "пресет" ANYWHERE the user can read it —
//     scanning rendered text rather than the i18n file, so a string that is
//     translated but never rendered (or rendered but never translated) is
//     caught either way. The presets dialog is opened for real, because it
//     holds most of the occurrences and starts closed.
//  2. The "?" marker is visible and its tooltip carries the Russian hint,
//     on hover AND on focus — the bubble is the only tooltip that works on
//     touch, so a hover-only affordance would be invisible on a phone.
//  3. Adding an element to .preset-field must not bring back the invisible
//     horizontal scroll of vts-nr4. That regression was caused by a
//     [data-tooltip]::after box — exactly what this change adds a new one of —
//     and it was invisible while the bubble was hidden, so the check is the
//     symptom itself (can the document be scrolled sideways?), measured both
//     with the bubble hidden and while it is actually shown.
import { startStubServer, launch, isVisible, screenshot } from "../harness.mjs";

export const name = "preset-renamed-hint";

const HINT_RU =
  "Сохранённый набор настроек задачи: язык, расшифровка, спикеры и промпты. " +
  "Выберите шаблон, чтобы заполнить все настройки сразу.";

// Presets the dialog will list, so its rows/badges render in Russian too.
const PRESETS = [
  { source: "system", id: "default", name: "По умолчанию", editable: false, is_default: true },
  { source: "user", id: "p1", name: "Совещание", editable: true, is_default: false },
];

// Phone widths where the sideways-scroll regression showed up.
const NARROW = [320, 360, 412];

// Measures the vts-nr4 symptom directly: ask the document to scroll right and
// see whether it moved. Reported alongside any element sticking past the edge,
// which is what tells you WHICH box did it.
const scrollProbe = (vw) => {
  const de = document.scrollingElement || document.documentElement;
  const before = de.scrollLeft;
  de.scrollLeft = 9999;
  const maxScrollLeft = de.scrollLeft;
  de.scrollLeft = before;

  const inScroller = (el) => {
    let n = el.parentElement;
    while (n && n !== document.body) {
      const ox = getComputedStyle(n).overflowX;
      if (ox === "auto" || ox === "scroll" || ox === "hidden") return true;
      n = n.parentElement;
    }
    return false;
  };

  const escapes = [];
  for (const el of document.querySelectorAll("body *")) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    if (cs.position === "fixed") continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > vw + 0.5 && !inScroller(el)) {
      const cls = typeof el.className === "string"
        ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".")
        : "";
      escapes.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${cls}@${Math.round(r.right)}`);
    }
  }
  return { maxScrollLeft, escapes: escapes.slice(0, 5) };
};

// Visible text of the page, with the bits the user never reads stripped out.
// data-tooltip is included on purpose: a tooltip still saying "пресет" is a
// user-visible miss even though it lives in an attribute.
const visibleTextProbe = () => {
  const chunks = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.tagName === "SCRIPT" || el.tagName === "STYLE") continue;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      for (const node of el.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          chunks.push(node.textContent.trim());
        }
      }
      for (const attr of ["data-tooltip", "title", "aria-label", "placeholder"]) {
        const v = el.getAttribute && el.getAttribute(attr);
        if (v) chunks.push(v);
      }
    }
  };
  walk(document);
  return chunks.join(" ‖ ");
};

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/presets": PRESETS });
  const browser = await launch();
  const failures = [];
  try {
    const page = await browser.newPage({
      viewport: { width: 1100, height: 700 },
      locale: "ru-RU",
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !m.text().includes("EventSource")) {
        errors.push("console.error: " + m.text());
      }
    });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.waitForTimeout(400);

    const lang = await page.evaluate(() => document.documentElement.lang);
    if (lang !== "ru") {
      failures.push(`document lang expected "ru", got "${lang}" — rest of this scenario is meaningless`);
    }

    // (1) The label itself.
    const label = await page.evaluate(
      () => document.querySelector(".preset-field .preset-label")?.textContent.trim() ?? null,
    );
    if (label !== "Шаблон") {
      failures.push(`preset label expected "Шаблон", got ${JSON.stringify(label)}`);
    }

    // (2) The "?" marker is really on screen (not just in the DOM).
    const hintPresent = await isVisible(page, ".preset-hint");
    if (!hintPresent) {
      failures.push(".preset-hint is not visible next to the preset selector");
    }

    // Tooltip text landed on both the marker and the select.
    for (const sel of [".preset-hint", "#preset-select"]) {
      const tip = await page.evaluate(
        (s) => document.querySelector(s)?.getAttribute("data-tooltip") ?? null,
        sel,
      );
      if (tip !== HINT_RU) {
        failures.push(`${sel} data-tooltip expected the ru hint, got ${JSON.stringify(tip)}`);
      }
    }

    // (3) The bubble actually appears — on hover and, separately, on focus.
    // A hint that only answers to hover is unreachable by touch and keyboard.
    const bubbleOpacity = async () =>
      page.evaluate(() => {
        const el = document.querySelector(".preset-hint");
        if (!el) return null;
        const cs = getComputedStyle(el, "::after");
        return { opacity: cs.opacity, visibility: cs.visibility };
      });

    // Guarded on the marker existing: with no element there is no bubble to
    // interrogate, and driving hover at a missing selector would abort the
    // scenario with a Playwright timeout instead of reporting what is wrong.
    if (hintPresent) {
      const hiddenState = await bubbleOpacity();
      if (hiddenState && hiddenState.visibility !== "hidden") {
        failures.push(`hint bubble should start hidden, got visibility=${hiddenState.visibility}`);
      }

      await page.hover(".preset-hint");
      await page.waitForTimeout(750); // 0.5s show-delay + transition
      const hovered = await bubbleOpacity();
      if (!hovered || hovered.visibility !== "visible" || hovered.opacity !== "1") {
        failures.push(`hint bubble did not appear on hover: ${JSON.stringify(hovered)}`);
      }

      await page.mouse.move(0, 0);
      await page.waitForTimeout(300);
      await page.evaluate(() => document.querySelector(".preset-hint").focus());
      await page.waitForTimeout(750);
      const focused = await bubbleOpacity();
      if (!focused || focused.visibility !== "visible") {
        failures.push(`hint bubble did not appear on keyboard focus: ${JSON.stringify(focused)}`);
      }
      await page.evaluate(() => document.querySelector(".preset-hint").blur());
    }

    // (4) No "пресет" anywhere the user can read — main form first.
    const formText = await page.evaluate(visibleTextProbe);
    const hitsForm = formText.match(/[Пп]ресет\w*/g);
    if (hitsForm) {
      failures.push(`main form still shows "пресет": ${[...new Set(hitsForm)].join(", ")}`);
    }

    // ...then the presets dialog, which holds most of the wording.
    await page.click("#header-menu-btn");
    await page.waitForTimeout(150);
    await page.click("#presets-btn");
    await page.waitForTimeout(400);
    const dlgOpen = await page.evaluate(
      () => !!document.getElementById("presets-dialog")?.open,
    );
    if (!dlgOpen) {
      failures.push("presets dialog did not open — its wording went unchecked");
    } else {
      const dlgText = await page.evaluate(visibleTextProbe);
      const hitsDlg = dlgText.match(/[Пп]ресет\w*/g);
      if (hitsDlg) {
        failures.push(`presets dialog still shows "пресет": ${[...new Set(hitsDlg)].join(", ")}`);
      }
      const title = await page.evaluate(
        () => document.querySelector("#presets-dialog h2")?.textContent.trim() ?? null,
      );
      if (title !== "Управление шаблонами") {
        failures.push(`presets dialog title expected "Управление шаблонами", got ${JSON.stringify(title)}`);
      }
      await screenshot(page, "preset-renamed-dialog-ru");
    }
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);

    await screenshot(page, "preset-renamed-hint-ru");
    failures.push(...errors);
    await page.close();

    // (5) The regression that this change is most likely to cause: a new
    // ::after bubble inside .preset-field re-introducing sideways scroll.
    for (const width of NARROW) {
      const p = await browser.newPage({ viewport: { width, height: 900 }, locale: "ru-RU" });
      await p.goto(baseUrl, { waitUntil: "networkidle" });
      await p.waitForTimeout(400);

      const idle = await p.evaluate(scrollProbe, width);
      if (idle.maxScrollLeft !== 0) {
        failures.push(
          `[${width}px] page scrolls horizontally by ${idle.maxScrollLeft}px with the hint bubble hidden ` +
          `(offenders: ${idle.escapes.join(", ") || "none visibly past the edge"})`,
        );
      }
      if (idle.escapes.length) {
        failures.push(`[${width}px] past the viewport edge: ${idle.escapes.join(", ")}`);
      }

      // And while the bubble is actually painted — the wide box only exists then.
      const hintThere = await isVisible(p, ".preset-hint");
      if (!hintThere) {
        failures.push(`[${width}px] .preset-hint is not visible on a phone-width layout`);
      } else {
        await p.hover(".preset-hint");
        await p.waitForTimeout(750);
        const shown = await p.evaluate(scrollProbe, width);
        if (shown.maxScrollLeft !== 0) {
          failures.push(
            `[${width}px] page scrolls horizontally by ${shown.maxScrollLeft}px while the hint bubble is SHOWN ` +
            `(offenders: ${shown.escapes.join(", ") || "none visibly past the edge"})`,
          );
        }
        if (shown.escapes.length) {
          failures.push(`[${width}px] bubble shown — past the viewport edge: ${shown.escapes.join(", ")}`);
        }

        // A bubble can be perfectly scroll-safe and still unreadable: content
        // overflowing to the LEFT never adds scrollWidth, so the probe above is
        // blind to it. The first cut of this change shipped exactly that — a
        // right-anchored 256px bubble on a trigger 85px from the edge, clipped
        // mid-word off-screen. Measure the painted box against the viewport.
        const bubble = await p.evaluate(() => {
          const el = document.querySelector(".preset-hint");
          if (!el) return null;
          const trigger = el.getBoundingClientRect();
          const cs = getComputedStyle(el, "::after");
          const w = parseFloat(cs.width);
          const h = parseFloat(cs.height);
          // ::after is positioned against the trigger's padding box; `left`
          // resolves to a used px value once painted.
          const offset = parseFloat(cs.left);
          const left = trigger.left + (Number.isNaN(offset) ? 0 : offset);
          return {
            left: Math.round(left),
            right: Math.round(left + w),
            width: Math.round(w),
            height: Math.round(h),
          };
        });
        if (bubble) {
          // Readability, not just containment. Squeezing the bubble until it
          // fits is always available and always wrong: capping it against the
          // 15px trigger produced a 15x230px one-word-per-line column that
          // passed every edge check here. A hint too narrow to read is not a
          // hint, so hold it to a sane minimum and a sane aspect.
          if (bubble.width < 140) {
            failures.push(
              `[${width}px] hint bubble is only ${bubble.width}px wide — too narrow to read`,
            );
          }
          if (bubble.height > bubble.width) {
            failures.push(
              `[${width}px] hint bubble is ${bubble.width}x${bubble.height} — taller than it is wide, ` +
              `text is wrapping one word per line`,
            );
          }
          if (bubble.left < -0.5) {
            failures.push(
              `[${width}px] hint bubble is clipped off the LEFT edge: starts at x=${bubble.left} ` +
              `(width ${bubble.width}px) — text is cut off and unreadable`,
            );
          }
          if (bubble.right > width + 0.5) {
            failures.push(
              `[${width}px] hint bubble runs past the RIGHT edge: ends at x=${bubble.right} of ${width}`,
            );
          }
        }
        if (width === 360) await screenshot(p, "preset-hint-bubble-360");
      }
      await p.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

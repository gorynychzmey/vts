// Regression (vts-nr4), two affordance details:
//
//  1. Header menu entries carry the same icons they had as toolbar buttons.
//     The label MUST live in an inner <span data-i18n>, not on the button —
//     applyI18n() assigns textContent to the [data-i18n] element, so putting it
//     on the button would delete the sibling <svg> on every language apply.
//     The push-notification entry is the sharp edge: its label is also rewritten
//     at runtime by setPushButtonState().
//  2. Icon buttons mark actionable vs not by the BORDER, not by opacity alone:
//     enabled ghost buttons have a visible outline + fill, disabled ones have
//     neither (transparent border and background).
//     The disabled sample now comes from a DISABLED-BY-STATE control rendered
//     for the check, because the task card no longer keeps one: pause/resume
//     became a single toggle in redesign v2 that hides when neither action
//     applies, rather than sitting there greyed out. The CSS rule is unchanged
//     and still worth pinning — only a live example had to be arranged.
import { startStubServer, launch } from "../harness.mjs";

export const name = "menu-icons-and-button-states";

const TASK = {
  id: "t1",
  status: "completed",
  state: "completed",
  title: "Make vibe coding ready for the enterprise",
  source_url: "https://event.on24.com/eventRegistration/console/apollox/mainEvent?x=1",
  source_type: "url",
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
  size_bytes: 5444141056,
  duration_sec: 2880,
  options: { transcript: true, audio_only: false, diarize: true },
  // Completed + restartable => a mix of enabled and disabled toolbar buttons,
  // which is what makes the two states comparable in one card.
  capabilities: { can_restart_summary: true, can_restart_final_summary: true },
  results: [{ id: "r1", kind: "summary", name: "Zusammenfassung" }],
  steps: [],
};

const transparent = (c) => c === "transparent" || /rgba\(\s*0,\s*0,\s*0,\s*0\s*\)/.test(c);

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK],
    "/api/push/config": { enabled: true },
  });
  const browser = await launch();
  const failures = [];
  try {
    // German: the longest labels, so a wrapped row also exercises icon alignment.
    for (const locale of ["en-US", "de-DE"]) {
      const context = await browser.newContext({ locale, viewport: { width: 412, height: 900 } });
      const page = await context.newPage();
      await page.goto(baseUrl, { waitUntil: "networkidle" });
      await page.waitForTimeout(600);

      // ---- 1. every visible menu entry has an icon AND a non-empty label ----
      await page.click("#header-menu-btn");
      await page.waitForTimeout(300);
      const items = await page.evaluate(() =>
        [...document.querySelectorAll("#header-menu button")]
          .filter((b) => b.offsetParent !== null)
          .map((b) => {
            const svg = b.querySelector("svg");
            const span = b.querySelector("span[data-i18n], span");
            return {
              id: b.id,
              hasSvg: !!svg,
              svgW: svg ? Math.round(svg.getBoundingClientRect().width) : 0,
              label: (span ? span.textContent : "").trim(),
              // The label must be in a child, not directly on the button, or
              // applyI18n would wipe the icon.
              labelInChild: !!span,
            };
          })
      );

      if (items.length < 5) {
        failures.push(`[${locale}] expected >=5 header menu entries, got ${items.length}`);
      }
      for (const it of items) {
        if (!it.hasSvg) failures.push(`[${locale}] menu entry ${it.id} has no icon`);
        else if (it.svgW < 10) failures.push(`[${locale}] menu entry ${it.id} icon collapsed to ${it.svgW}px`);
        if (!it.label) failures.push(`[${locale}] menu entry ${it.id} has an empty label`);
        if (!it.labelInChild) {
          failures.push(`[${locale}] menu entry ${it.id} label is not in a child span — applyI18n will delete its icon`);
        }
      }

      // Every icon in a menu is drawn the same way: a 2px stroke on no fill.
      // The task menu had two leftovers from the old toolbar drawn as SOLID
      // filled paths (the rename pencil, the player triangle), so they read
      // noticeably heavier than the rows around them — the "our icons are worse
      // than the prototype's" note in docs/design-v2/REMAINING.md.
      // A deliberately filled DETAIL inside an outlined glyph is fine (the play
      // triangle inside the player's screen), so this looks at the OUTER shape:
      // the svg's own computed fill, which is what makes a glyph look solid.
      // The task kebab has to be OPEN for its icons to have a size — a closed
      // menu measures 0 and would be filtered out, leaving this check silently
      // covering only the header (which is not where the filled icons were).
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);
      await page.click(".task .task-menu-btn");
      await page.waitForTimeout(300);
      const taskIconsVisible = await page.evaluate(
        () => [...document.querySelectorAll(".task-menu .menu-item svg")]
          .filter((svg) => svg.getBoundingClientRect().width > 0).length
      );
      if (!taskIconsVisible) {
        failures.push(`[${locale}] task menu icons are not measurable — the fill check would be vacuous`);
      }

      const solid = await page.evaluate(() =>
        [...document.querySelectorAll("#header-menu button svg, .task-menu .menu-item svg")]
          .filter((svg) => svg.getBoundingClientRect().width > 0)
          .filter((svg) => {
            const f = getComputedStyle(svg).fill;
            return f !== "none" && f !== "rgba(0, 0, 0, 0)";
          })
          .map((svg) => (svg.closest("button, a")?.className || "").trim() || "header-menu entry")
      );
      if (solid.length) {
        failures.push(
          `[${locale}] menu icons drawn as filled shapes among stroked ones: ${JSON.stringify(solid)}`
        );
      }

      // Re-applying i18n must not destroy the icons (the actual failure mode).
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);
      await page.click("#header-menu-btn");
      await page.waitForTimeout(300);
      const survived = await page.evaluate(() => {
        if (typeof applyI18n === "function") applyI18n(document);
        return [...document.querySelectorAll("#header-menu button")]
          .filter((b) => b.offsetParent !== null)
          .every((b) => !!b.querySelector("svg"));
      });
      if (!survived) {
        failures.push(`[${locale}] icons disappeared from the menu after applyI18n() re-ran`);
      }

      await page.keyboard.press("Escape");
      await page.click("body", { position: { x: 5, y: 5 } });
      await page.waitForTimeout(200);

      // ---- 2. enabled vs disabled icon buttons differ by border ----
      const states = await page.evaluate(() => {
        const card = document.querySelector("article.task");
        const out = { enabled: [], disabled: [] };
        for (const b of card.querySelectorAll(".task-actions-inline .icon-btn.ghost")) {
          if (b.getBoundingClientRect().width === 0) continue;
          const cs = getComputedStyle(b);
          const rec = { cls: [...b.classList].join("."), border: cs.borderTopColor, bg: cs.backgroundColor };
          (b.disabled ? out.disabled : out.enabled).push(rec);
        }
        return out;
      });

      if (!states.enabled.length) failures.push(`[${locale}] no enabled ghost icon button found to check`);
      if (!states.disabled.length) {
        // No card button is disabled-and-visible any more, so disable one for
        // the measurement. Still a real assertion: it reads the computed style
        // the live CSS produces for .icon-btn.ghost:disabled.
        const sampled = await page.evaluate(() => {
          const btn = document.querySelector("article.task .task-actions-inline .icon-btn.ghost:not(.hidden)");
          if (!btn) return null;
          btn.disabled = true;
          const cs = getComputedStyle(btn);
          const rec = { cls: [...btn.classList].join("."), border: cs.borderTopColor, bg: cs.backgroundColor };
          btn.disabled = false;
          return rec;
        });
        if (sampled) states.disabled.push(sampled);
        else failures.push(`[${locale}] no ghost icon button on the card to sample the disabled style from`);
      }

      for (const b of states.enabled) {
        if (transparent(b.border)) {
          failures.push(`[${locale}] enabled button ${b.cls} has no visible border (${b.border})`);
        }
        if (transparent(b.bg)) {
          failures.push(`[${locale}] enabled button ${b.cls} has no fill (${b.bg})`);
        }
      }
      for (const b of states.disabled) {
        if (!transparent(b.border)) {
          failures.push(`[${locale}] disabled button ${b.cls} still draws a border (${b.border}) — the outline should mark actionable only`);
        }
      }

      await page.close();
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

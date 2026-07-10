// Verifies VOS-73 informative tooltips end-to-end per locale (ru/en/de):
// the browser locale drives detectLocale(), the locale script loads, and the
// new title texts land on the real task-card buttons, the restart-summary
// menu items (titles set via JS next to textContent), the preset save button
// and the admin controls. Samples one representative string per surface;
// byte-level completeness is covered by tests/test_i18n_tooltip_keys.py.
import { startStubServer, launch, screenshot } from "../harness.mjs";

export const name = "tooltip-i18n-texts";

const COMPLETED_TASK = {
  id: "22222222-2222-2222-2222-222222222222",
  source_url: "http://x/v", source_title: "Tooltip probe",
  status: "completed", summary_path: "/x/summary/final.md", media_path: "/x/media.mp4",
  options: {
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "summarize_final", status: "completed", started_at: "2026-07-10T10:01:00Z", finished_at: "2026-07-10T10:02:00Z" },
  ],
  created_at: "2026-07-10T10:00:00Z", updated_at: "2026-07-10T10:02:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 1, total: 1 } }, stats: {},
};

// selector -> expected title per locale. Values must match vts/static/i18n/*.js.
const EXPECTED = {
  ".delete-btn": {
    ru: "Удалить задачу со всеми файлами — безвозвратно",
    en: "Delete the task and all its files — cannot be undone",
    de: "Aufgabe mit allen Dateien löschen – kann nicht rückgängig gemacht werden",
  },
  ".archive-btn": {
    ru: "Архивировать: убрать из списка и удалить медиа; транскрипт и сводка сохранятся",
    en: "Archive: remove from the list and delete media; transcript and summary are kept",
    de: "Archivieren: aus der Liste entfernen und Medien löschen; Transkript und Zusammenfassung bleiben erhalten",
  },
  ".pause-btn": {
    ru: "Приостановить обработку после текущего шага",
    en: "Pause processing after the current step",
    de: "Verarbeitung nach dem aktuellen Schritt anhalten",
  },
  ".download-media-btn": {
    ru: "Скачать исходный медиафайл",
    en: "Download the original media file",
    de: "Originale Mediendatei herunterladen",
  },
  ".restart-summary-full-btn": {
    ru: "Пересчитать сводку заново по всем частям транскрипта",
    en: "Recompute the summary from scratch over the whole transcript",
    de: "Zusammenfassung komplett aus dem gesamten Transkript neu berechnen",
  },
  ".restart-summary-final-btn": {
    ru: "Пересобрать только итоговую сводку из уже готовых частей",
    en: "Rebuild only the final summary from already processed parts",
    de: "Nur die finale Zusammenfassung aus bereits verarbeiteten Teilen neu erstellen",
  },
  "#preset-save-btn": {
    ru: "Сохранить текущие настройки задачи как новый пресет",
    en: "Save the current task settings as a new preset",
    de: "Aktuelle Aufgabeneinstellungen als neues Preset speichern",
  },
  "#admin-user-select": {
    ru: "Выбрать пользователя, от имени которого работать",
    en: "Choose a user to act as",
    de: "Benutzer auswählen, als der gearbeitet wird",
  },
  "#admin-apply-btn": {
    ru: "Работать от имени выбранного пользователя",
    en: "Act as the selected user",
    de: "Als ausgewählter Benutzer arbeiten",
  },
  "#admin-reset-btn": {
    ru: "Вернуться к работе от своего имени",
    en: "Switch back to your own account",
    de: "Zum eigenen Benutzer zurückkehren",
  },
};

const BROWSER_LOCALE = { ru: "ru-RU", en: "en-US", de: "de-DE" };

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [COMPLETED_TASK],
  });
  const browser = await launch();
  const failures = [];
  try {
    for (const locale of ["ru", "en", "de"]) {
      const page = await browser.newPage({
        viewport: { width: 1100, height: 700 },
        locale: BROWSER_LOCALE[locale],
      });
      const errors = [];
      page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
      await page.goto(baseUrl, { waitUntil: "networkidle" });
      await page.waitForTimeout(300);

      const applied = await page.evaluate(() => document.documentElement.lang);
      if (applied !== locale) {
        failures.push(`[${locale}] document lang expected "${locale}", got "${applied}" — locale detection broken`);
      }
      for (const [selector, byLocale] of Object.entries(EXPECTED)) {
        const title = await page.evaluate(
          (sel) => document.querySelector(sel)?.getAttribute("title") ?? null,
          selector,
        );
        if (title !== byLocale[locale]) {
          failures.push(`[${locale}] ${selector} title expected "${byLocale[locale]}", got "${title}"`);
        }
      }
      failures.push(...errors.map((e) => `[${locale}] ${e}`));
      if (locale === "ru") await screenshot(page, "tooltip-i18n-ru");
      await page.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

# Informative Button Tooltips (VOS-73 / vts-3rb) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite button tooltips in the VTS web UI to the differentiated style from the spec (`docs/superpowers/specs/2026-07-10-informative-tooltips-design.md`), add the four missing tooltips, and lock consistency with a static-invariant test.

**Architecture:** All tooltip texts live in three locale dictionaries (`vts/static/i18n/{ru,en,de}.js`, JSON-parseable object literals). `index.html` carries `data-i18n-title` attributes plus static English `title=` fallbacks; `app.js` assigns some titles dynamically via `t("key")`. A new pytest module (same pattern as `tests/test_static_css_invariants.py`) asserts every tooltip key exists in all three locales and every static fallback matches `en.js`.

**Tech Stack:** Vanilla JS static frontend, Python/pytest for invariants, Playwright harness (`tests/ui/`) via the `verifier-web` skill for browser verification.

## Global Constraints

- Locale files must stay JSON-parseable: double-quoted keys/values, `"key": "value",` per line, no trailing comma after the last entry.
- RU term is «сводка» (never "summary" inside RU strings); DE term is „Zusammenfassung".
- Named objects everywhere: «Удалить промпт», not bare «Удалить».
- Ellipsis is the single char `…`, not three dots.
- Static `title=` fallbacks in `index.html` must equal the `en.js` value of the element's `data-i18n-title` key (enforced by the Task 1 test).
- Version bump policy: bump `vts/__init__.py` (`1.1.21` → `1.1.22`) once, in the first code commit (Task 1). No build tag unless Victor explicitly asks.
- Every task ends with commit AND push (project rule).

---

### Task 1: Invariant test for tooltip i18n consistency + version bump

**Model:** Sonnet 5 — exact file and complete test code provided, deterministic pass/fail.

**Files:**
- Create: `tests/test_i18n_tooltip_keys.py`
- Modify: `vts/__init__.py:3` (version bump)

**Interfaces:**
- Produces: pytest module `tests/test_i18n_tooltip_keys.py` with tests `test_every_tooltip_key_exists_in_all_locales` and `test_static_title_fallbacks_match_en`. Tasks 2–3 rely on these tests to validate their edits; Task 3 relies on the extractor picking up new `data-i18n-title` attributes and new `.title = t("…")` lines automatically.

- [ ] **Step 1: Write the test file**

Create `tests/test_i18n_tooltip_keys.py` with exactly:

```python
from __future__ import annotations

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "vts" / "static"

# Keys assigned to element.title via an intermediate variable in app.js,
# which the line-based extractor below cannot see (e.g. app.js ~1678:
# `const label = expanded ? t("action.collapse") : t("action.expand")`).
EXTRA_TITLE_KEYS = {"action.collapse"}


def _load_locale(name: str) -> dict[str, str]:
    src = (STATIC / "i18n" / f"{name}.js").read_text(encoding="utf-8")
    match = re.search(r"window\.__VTS_I18N\.\w+ = (\{.*\});", src, re.S)
    assert match, f"cannot locate the dictionary literal in {name}.js"
    return json.loads(match.group(1))


LOCALES = {name: _load_locale(name) for name in ("ru", "en", "de")}


def _tooltip_keys_from_index() -> set[str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return set(re.findall(r'data-i18n-title="([^"]+)"', html))


def _tooltip_keys_from_app_js() -> set[str]:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in js.splitlines():
        if re.search(r'\.title\s*=|setAttribute\("title"', line):
            keys.update(re.findall(r't\("([a-z0-9_.]+)"\)', line))
    return keys


def test_every_tooltip_key_exists_in_all_locales() -> None:
    keys = _tooltip_keys_from_index() | _tooltip_keys_from_app_js() | EXTRA_TITLE_KEYS
    assert len(keys) > 20, f"extractor found only {len(keys)} keys — regressed?"
    missing = {
        locale: sorted(key for key in keys if key not in dictionary)
        for locale, dictionary in LOCALES.items()
    }
    missing = {locale: keys_ for locale, keys_ in missing.items() if keys_}
    assert not missing, f"tooltip keys missing from locale dictionaries: {missing}"


def test_static_title_fallbacks_match_en() -> None:
    """Elements carrying both a static title= fallback and data-i18n-title
    must keep the fallback equal to the en.js value, so the pre-i18n first
    paint shows the same wording English users get after i18n applies."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    en = LOCALES["en"]
    mismatches: list[tuple[str, str, str]] = []
    for tag in re.finditer(r"<[a-zA-Z][^>]*>", html, re.S):
        text = tag.group(0)
        title_match = re.search(r'(?<!-)\btitle="([^"]*)"', text)
        key_match = re.search(r'data-i18n-title="([^"]+)"', text)
        if not (title_match and key_match):
            continue
        expected = en.get(key_match.group(1))
        if expected is not None and title_match.group(1) != expected:
            mismatches.append((key_match.group(1), title_match.group(1), expected))
    assert not mismatches, (
        "static title= fallbacks out of sync with en.js "
        f"(key, fallback, en): {mismatches}"
    )
```

- [ ] **Step 2: Run the new tests**

Run: `python -m pytest tests/test_i18n_tooltip_keys.py -v`

Expected: both tests PASS against the current tree (all existing keys are present and fallbacks currently match en.js). If `test_static_title_fallbacks_match_en` reports pre-existing drift, fix the `title=` fallback in `index.html` to match the current `en.js` value (do NOT change en.js in this task) and re-run until green.

- [ ] **Step 3: Bump version**

In `vts/__init__.py` change `__version__ = "1.1.21"` to `__version__ = "1.1.22"`.

- [ ] **Step 4: Run the full test suite**

Run: `python -m pytest tests/ -x -q --ignore=tests/ui`
Expected: PASS (no regressions; `test_version.py` must still pass after the bump).

- [ ] **Step 5: Commit and push**

```bash
git add tests/test_i18n_tooltip_keys.py vts/__init__.py
git commit -m "test: i18n tooltip-key consistency invariant + bump to 1.1.22 (vts-3rb)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```

---

### Task 2: Rewrite existing tooltip texts in ru/en/de and sync index.html fallbacks

**Model:** Sonnet 5 — pure text substitution, exact strings below, verified by the Task 1 test.

**Files:**
- Modify: `vts/static/i18n/ru.js`, `vts/static/i18n/en.js`, `vts/static/i18n/de.js`
- Modify: `vts/static/index.html` (static `title=` fallbacks only)
- Test: `tests/test_i18n_tooltip_keys.py` (from Task 1, no changes)

**Interfaces:**
- Consumes: Task 1 tests.
- Produces: final wording for existing keys; Task 4 verifies a sample in the browser.

- [ ] **Step 1: Update values of existing keys in all three locale files**

For each key below, replace the value in the corresponding file. Keys not listed stay unchanged (`action.create`, `tokens.close`, `tab.*`, `tasks.media_expired_tooltip`, `prompts.manage.open`, `preset.manage.open`).

`vts/static/i18n/ru.js`:

```
"action.delete": "Удалить задачу со всеми файлами — безвозвратно",
"action.archive": "Архивировать: убрать из списка и удалить медиа; транскрипт и сводка сохранятся",
"action.pause": "Приостановить обработку после текущего шага",
"action.resume": "Продолжить обработку с места остановки",
"action.restart_summary": "Перезапустить сводку…",
"admin.switch_user": "Выбрать пользователя, от имени которого работать",
"admin.apply": "Работать от имени выбранного пользователя",
"admin.use_self": "Вернуться к работе от своего имени",
"action.copy_tab": "Скопировать содержимое открытой вкладки в буфер обмена",
"action.save_tab": "Скачать содержимое открытой вкладки файлом",
"action.download_media": "Скачать исходный медиафайл",
"action.enable_notifications": "Включить браузерные уведомления о завершении задач",
"preset.manage.make_default": "Использовать этот пресет по умолчанию для новых задач",
"about.open": "Показать параметры и детали задачи",
"tokens.open": "Управление API-токенами",
"action.refresh": "Обновить список задач",
"action.expand": "Развернуть подробности задачи",
"action.collapse": "Свернуть подробности задачи",
"action.edit_name": "Переименовать задачу",
"action.save_name": "Сохранить новое имя",
"action.cancel_edit": "Отменить переименование",
"action.logout": "Выйти из аккаунта",
"prompts.manage.edit": "Изменить промпт",
"prompts.manage.delete": "Удалить промпт",
"prompts.manage.duplicate": "Дублировать промпт",
"preset.manage.edit": "Изменить пресет",
"preset.manage.delete": "Удалить пресет",
"preset.manage.duplicate": "Дублировать пресет",
```

`vts/static/i18n/en.js`:

```
"action.delete": "Delete the task and all its files — cannot be undone",
"action.archive": "Archive: remove from the list and delete media; transcript and summary are kept",
"action.pause": "Pause processing after the current step",
"action.resume": "Resume processing from where it stopped",
"action.restart_summary": "Restart summary…",
"admin.switch_user": "Choose a user to act as",
"admin.apply": "Act as the selected user",
"admin.use_self": "Switch back to your own account",
"action.copy_tab": "Copy the open tab's content to the clipboard",
"action.save_tab": "Download the open tab's content as a file",
"action.download_media": "Download the original media file",
"action.enable_notifications": "Enable browser notifications when tasks finish",
"preset.manage.make_default": "Use this preset by default for new tasks",
"about.open": "Show task settings and details",
"tokens.open": "Manage API tokens",
"action.refresh": "Refresh the task list",
"action.expand": "Expand task details",
"action.collapse": "Collapse task details",
"action.edit_name": "Rename task",
"action.save_name": "Save the new name",
"action.cancel_edit": "Cancel renaming",
"action.logout": "Log out",
"prompts.manage.edit": "Edit prompt",
"prompts.manage.delete": "Delete prompt",
"prompts.manage.duplicate": "Duplicate prompt",
"preset.manage.edit": "Edit preset",
"preset.manage.delete": "Delete preset",
"preset.manage.duplicate": "Duplicate preset",
```

`vts/static/i18n/de.js`:

```
"action.delete": "Aufgabe mit allen Dateien löschen – kann nicht rückgängig gemacht werden",
"action.archive": "Archivieren: aus der Liste entfernen und Medien löschen; Transkript und Zusammenfassung bleiben erhalten",
"action.pause": "Verarbeitung nach dem aktuellen Schritt anhalten",
"action.resume": "Verarbeitung an der angehaltenen Stelle fortsetzen",
"action.restart_summary": "Zusammenfassung neu erstellen…",
"admin.switch_user": "Benutzer auswählen, als der gearbeitet wird",
"admin.apply": "Als ausgewählter Benutzer arbeiten",
"admin.use_self": "Zum eigenen Benutzer zurückkehren",
"action.copy_tab": "Inhalt des geöffneten Tabs in die Zwischenablage kopieren",
"action.save_tab": "Inhalt des geöffneten Tabs als Datei herunterladen",
"action.download_media": "Originale Mediendatei herunterladen",
"action.enable_notifications": "Browser-Benachrichtigungen bei Aufgabenabschluss aktivieren",
"preset.manage.make_default": "Dieses Preset als Standard für neue Aufgaben verwenden",
"about.open": "Einstellungen und Details der Aufgabe anzeigen",
"tokens.open": "API-Tokens verwalten",
"action.refresh": "Aufgabenliste aktualisieren",
"action.expand": "Aufgabendetails ausklappen",
"action.collapse": "Aufgabendetails einklappen",
"action.edit_name": "Aufgabe umbenennen",
"action.save_name": "Neuen Namen speichern",
"action.cancel_edit": "Umbenennen abbrechen",
"action.logout": "Abmelden",
"prompts.manage.edit": "Prompt bearbeiten",
"prompts.manage.delete": "Prompt löschen",
"prompts.manage.duplicate": "Prompt duplizieren",
"preset.manage.edit": "Preset bearbeiten",
"preset.manage.delete": "Preset löschen",
"preset.manage.duplicate": "Preset duplizieren",
```

Note: `prompts.manage.edit` / `preset.manage.edit` also serve as the submit-button
label in the editor forms (app.js:3157, app.js:3354, index.html:607). The label
change to «Изменить промпт» / «Изменить пресет» is intentional per the spec.

- [ ] **Step 2: Run the invariant tests — expect fallback mismatch failures**

Run: `python -m pytest tests/test_i18n_tooltip_keys.py -v`
Expected: `test_static_title_fallbacks_match_en` FAILS listing every static `title=` in `index.html` whose en value changed (delete, archive, pause, resume, restart_summary, switch_user, apply, use_self, copy_tab, save_tab, download_media, enable_notifications, about.open, tokens.open, refresh, expand, logout). `test_every_tooltip_key_exists_in_all_locales` PASSES.

- [ ] **Step 3: Sync static fallbacks in index.html**

For every mismatch reported in Step 2, set the element's `title="…"` in
`vts/static/index.html` to the new `en.js` value (the test output gives the
exact expected string per key). The affected attributes are near lines 32, 80,
96, 121, 128, 141, 250, 286, 300, 317, 329, 342, 360, 375, 390, 441, 454 of
the current file.

- [ ] **Step 4: Run the invariant tests again**

Run: `python -m pytest tests/test_i18n_tooltip_keys.py -v`
Expected: both PASS.

- [ ] **Step 5: Commit and push**

```bash
git add vts/static/i18n/ru.js vts/static/i18n/en.js vts/static/i18n/de.js vts/static/index.html
git commit -m "feat(ui): informative differentiated button tooltips in ru/en/de (vts-3rb)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```

---

### Task 3: New tooltips — preset save buttons and restart-summary menu items

**Model:** Sonnet 5 — exact edit points and complete snippets below.

**Files:**
- Modify: `vts/static/index.html:210,238` (preset buttons)
- Modify: `vts/static/app.js:1650-1653` (menu item titles)
- Modify: `vts/static/i18n/ru.js`, `en.js`, `de.js` (4 new keys each)
- Test: `tests/test_i18n_tooltip_keys.py` (from Task 1, no changes)

**Interfaces:**
- Consumes: Task 1 extractor (picks up the new `data-i18n-title` attributes and the new `.title = t("…")` lines automatically); locale files as left by Task 2.
- Produces: keys `preset.save_as_tooltip`, `preset.resave_tooltip`, `action.restart_summary_full_tooltip`, `action.restart_summary_final_tooltip` in all three locales.

- [ ] **Step 1: Add markup and JS title assignments (test-first: this makes the invariant test fail)**

In `vts/static/index.html`, extend the two preset buttons (currently lines 210 and 238):

```html
<button type="button" id="preset-save-btn" class="btn-text" data-i18n="preset.save_as"
        title="Save the current task settings as a new preset"
        data-i18n-title="preset.save_as_tooltip">
```

```html
<button type="button" id="preset-resave-btn" class="btn-text" data-i18n="preset.resave"
        title="Overwrite the selected preset with the current settings"
        data-i18n-title="preset.resave_tooltip">
```

(Keep each button's existing attributes and inner content; only add the two
title attributes. Match the file's existing multi-line attribute formatting.)

In `vts/static/app.js`, immediately after the existing `textContent`
assignments (currently lines 1650–1653):

```js
    if (restartSummaryFullBtn) {
      restartSummaryFullBtn.textContent = t("action.restart_summary_full");
      restartSummaryFullBtn.title = t("action.restart_summary_full_tooltip");
    }
    if (restartSummaryFinalBtn) {
      restartSummaryFinalBtn.textContent = t("action.restart_summary_final");
      restartSummaryFinalBtn.title = t("action.restart_summary_final_tooltip");
    }
```

(Preserve the file's existing null-guard structure around those assignments —
add only the two `.title` lines inside the existing guards.)

- [ ] **Step 2: Run the invariant test to verify it fails**

Run: `python -m pytest tests/test_i18n_tooltip_keys.py::test_every_tooltip_key_exists_in_all_locales -v`
Expected: FAIL — the four new keys reported missing from ru, en, and de.

- [ ] **Step 3: Add the four keys to each locale file**

`vts/static/i18n/ru.js` (place next to the existing `preset.save_as` /
`action.restart_summary_full` keys respectively):

```
"preset.save_as_tooltip": "Сохранить текущие настройки задачи как новый пресет",
"preset.resave_tooltip": "Записать текущие настройки в выбранный пресет",
"action.restart_summary_full_tooltip": "Пересчитать сводку заново по всем частям транскрипта",
"action.restart_summary_final_tooltip": "Пересобрать только итоговую сводку из уже готовых частей",
```

`vts/static/i18n/en.js`:

```
"preset.save_as_tooltip": "Save the current task settings as a new preset",
"preset.resave_tooltip": "Overwrite the selected preset with the current settings",
"action.restart_summary_full_tooltip": "Recompute the summary from scratch over the whole transcript",
"action.restart_summary_final_tooltip": "Rebuild only the final summary from already processed parts",
```

`vts/static/i18n/de.js`:

```
"preset.save_as_tooltip": "Aktuelle Aufgabeneinstellungen als neues Preset speichern",
"preset.resave_tooltip": "Ausgewähltes Preset mit den aktuellen Einstellungen überschreiben",
"action.restart_summary_full_tooltip": "Zusammenfassung komplett aus dem gesamten Transkript neu berechnen",
"action.restart_summary_final_tooltip": "Nur die finale Zusammenfassung aus bereits verarbeiteten Teilen neu erstellen",
```

- [ ] **Step 4: Run the invariant tests and the full suite**

Run: `python -m pytest tests/test_i18n_tooltip_keys.py -v && python -m pytest tests/ -x -q --ignore=tests/ui`
Expected: all PASS.

- [ ] **Step 5: Commit and push**

```bash
git add vts/static/index.html vts/static/app.js vts/static/i18n/ru.js vts/static/i18n/en.js vts/static/i18n/de.js
git commit -m "feat(ui): tooltips for preset save buttons and restart-summary menu (vts-3rb)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```

---

### Task 4: Browser verification and close-out

**Model:** Opus 4.8 — browser driving and judgment about what "looks right" needs interpretation, not a fixed script. (Executed inline by the main session if subagents are not used for verification.)

**Files:**
- No source changes expected. If verification uncovers a defect, fix it in the file it belongs to, re-run the Task 1 tests, and amend with a `fix(ui):` commit.

**Interfaces:**
- Consumes: the complete implementation from Tasks 1–3.

- [ ] **Step 1: Run the verifier-web skill**

Invoke the `verifier-web` skill (boots the real static frontend against stubbed `/api/*` in Chromium). Verify, for each locale ru → en → de:

1. Task-card icon buttons expose the new `title` values: delete, archive, pause, download media («Удалить задачу со всеми файлами — безвозвратно» etc. per the spec tables).
2. The restart-summary menu items carry the new tooltips distinguishing full vs final-only restart.
3. `#preset-save-btn` shows the save-as-preset tooltip.
4. Admin bar: apply / use-self / user-select tooltips.
5. Editor submit buttons now read «Изменить промпт» / «Изменить пресет» (intentional label change) and nothing is visually broken by longer strings.

Expected: all checks pass in all three locales.

- [ ] **Step 2: Run the full test suite one last time**

Run: `python -m pytest tests/ -q --ignore=tests/ui`
Expected: PASS.

- [ ] **Step 3: Close out**

```bash
bd close vts-3rb --reason="Tooltips rewritten per spec; invariant test added; verified in browser (ru/en/de)"
bd dolt push
git status   # must be clean and up to date with origin
```

Then (main session, not a subagent): update Linear VOS-73 — comment with the
delivered wording summary and move to In Review. No build tag unless Victor
asks for one.

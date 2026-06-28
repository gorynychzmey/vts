# Presets in the restart dialog (vts-2or extension)

**Status:** Design approved, ready for implementation plan
**Date:** 2026-06-28
**Builds on:** vts-2or (restart final with prompts) + vts-hp7 (task option presets).

## Проблема

Диалог перезапуска финала («Restart final with prompts») даёт выбрать набор
промптов вручную. Раз появились пресеты, логично дать быстрый способ
подставить набор промптов из пресета.

## Концепция

Чисто фронтендное расширение. Бэкенд НЕ трогаем — эндпоинт
`POST /api/tasks/{id}/restart_summary` уже принимает `{mode:"final_only",
prompts}`.

Перезапуск финала переразворачивает только финальную стадию по уже
обработанному транскрипту, поэтому из пресета берутся **только**
`options.prompts`. Поля `language` / `audio_only` / `transcript` неприменимы
(транскрипт готов, режим/язык менять поздно) и игнорируются.

Семантика: диалог по умолчанию показывает текущий набор задачи; пресет —
ярлык, подставляющий его промпты в мультиселект.

## UI

В диалоге `#restart-final-dialog` (см.
[vts/static/index.html](../../../vts/static/index.html)):

- Над флэт-мультиселектом (`#restart-final-select`) добавляется
  `<select id="restart-final-preset">`.
- **При открытии** (`openRestartFinalDialog`, app.js):
  - дропдаун на нейтральном пункте «—» (`restart_final.preset_none`);
  - мультиселект предзаполнен **текущим набором задачи** (поведение не
    меняется: `task.options.prompts` или `[{system,summary}]`).
  - дропдаун наполняется из `presetsCache` (его заполняет `loadPresets()` при
    bootstrap); если кэш пуст — вызвать `loadPresets()` перед заполнением.
    Системные пресеты локализуются по id (`t("preset.system."+id)`), как везде.
- **Выбор пресета** → взять `preset.options.prompts`, отфильтровать висячие
  пользовательские ссылки против загруженного `/api/prompts` (системные всегда
  валидны), перерисовать мультиселект этим набором
  (`renderPromptMultiselect(restartFinalSelect, prompts, filtered, {flat:true})`),
  обновить состояние кнопки submit (`updateRestartFinalSubmitState`).
- **Возврат на «—»** — без эффекта (пользователь уже может править чекбоксы
  вручную).
- **Submit** — без изменений: шлёт `getSelectedFrom(restartFinalSelect)` как
  `prompts`.

Дропдаун пресетов в диалоге сбрасывается на «—» при каждом открытии.

## Переиспользование

- `presetsCache` + `loadPresets()` (vts-hp7).
- `renderPromptMultiselect(container, prompts, selectedRefs, {flat:true})`.
- Фильтрация висячих промптов — та же логика, что применяется на форме
  создания при применении пресета (`filterDanglingPrompts`-style: drop user
  refs not in the loaded prompts list, keep system refs).
- `t()`, локализация имени системного пресета по id.

## i18n

Новые ключи во всех трёх локалях (`vts/static/i18n/{en,ru,de}.js`):

- `restart_final.preset` — лейбл дропдауна ("Preset" / "Пресет" / "Preset").
- `restart_final.preset_none` — нейтральный пункт ("—" во всех локалях).

## Тестирование

- **verifier-web** — расширить `tests/ui/scenarios/restart-dialog.mjs` (или
  добавить шаги): override `/api/presets` с одним пресетом; открыть диалог из
  меню задачи; убедиться, что `#restart-final-preset` присутствует и по
  умолчанию на «—», мультиселект = текущий набор задачи; выбрать пресет в
  дропдауне → чекбоксы мультиселекта стали = промпты пресета (с фильтрацией
  висячих, если есть). Closed-state диалога уже покрыт существующим сценарием.
- `node --check` на app.js + 3 локали.

## Out of scope

- Применение `language` / `audio_only` / `transcript` из пресета (неприменимы
  к перезапуску финала).
- Любые изменения бэкенда / эндпоинта перезапуска.
- Пресеты в каких-либо других местах.

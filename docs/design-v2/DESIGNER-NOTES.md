> Записка дизайнера (Claude Design), раунд 2, принята 2026-08-17.
> Файлы в этой папке: `prototype-v2.dc.html` — прототип, `vts-theme.css` —
> готовый CSS. Первый раунд и бриф удалены как отработанные.

# Round 2 — записка по классам и токенам

## Что отдаём

- `VTS Redesign v2.dc.html` — прототип на классах (инлайн остался только на
  динамических значениях: `width` прогресс-баров, `stroke-dashoffset` кольца
  загрузки, `opacity` галочки в списке пресетов, пара `margin-left:auto`).
- `vts-theme.css` — забирается напрямую: `:root` со всеми токенами, тёмная тема
  (ручная + `prefers-color-scheme`), классы компонентов, адаптив.

## Переиспользованные ваши классы

Каркас и контролы: `layout`, `card`, `icon-btn`, `ghost`, `primary`, `btn-text`,
`mono`, `sr-only`, `hidden`, `active`, `disabled`, `header-menu`,
`header-menu-wrapper`, `header-version`, `user-context`, `context-line`,
`context-label`, `admin-controls`, `new-task-header`, `source-type-radio-group`,
`url-row`, `options-row`, `option-pill`, `language-control`,
`prompt-select-field`, `prompt-select-label`, `prompt-select`, `preset-field`,
`preset-label`, `preset-edit-options`, `preset-dangling-hint`.

Задачи: `task`, `task-list`, `task-header-row`, `task-main`, `task-title-row`,
`task-link`, `task-source`, `task-stats`, `task-stats-chip`, `task-status`,
`task-runtime`, `task-actions-inline`, `task-toolbar-wrap`, `task-body`,
`task-message`, `task-expired`, `task-empty`, `task-filters`, `filter-search`,
`filter-type`, `filter-range`, `filter-date`, `filter-range-dash`,
`task-sentinel`, `task-sentinel-spinner`, `task-sentinel-end`,
`new-tasks-banner`, `new-tasks-count`, `task-name-edit`, `task-name-input`,
`task-name-ok-btn`, `task-name-cancel-btn`, `task-edit-name-btn`,
`task-player-btn`, `toggle-btn`, `pause-btn`, `resume-btn`, `archive-btn`,
`delete-btn`, `download-media-btn`, `restart-summary-btn`,
`restart-summary-full-btn`, `restart-summary-final-btn`, `btn-menu`,
`btn-menu-wrapper`, `resolve-voices-btn`.

Прогресс и табы: `step-progress`, `step-progress-fill`, `step-progress-text`,
`local-progress`, `overall-progress`, `progress-stack`, `progress-group`,
`progress-caption`, `step-label`, `step-meta`, `step-time`, `tabs-bar`,
`tab-btn`, `tab-actions`, `tab-content`, `tab-copy-btn`, `tab-save-btn`.

Диалоги: `tokens-dialog`, `tokens-dialog-header`, `tokens-list`, `tokens-help`,
`tokens-create-form`, `prompts-dialog`, `prompts-list`, `prompt-form`,
`prompt-form-actions`, `prompt-body-input`, `presets-dialog`, `presets-list`,
`delivery-dialog`, `delivery-sections`, `delivery-section`, `delivery-tabs-bar`,
`delivery-adapter-label`, `delivery-check-message`, `delivery-empty-hint`,
`delivery-select-field`, `about-grid`, `about-row`, `about-label`,
`about-value`, `about-params`, `about-timings`, `about-title-row`,
`voice-resolution-dialog`, `voice-list`, `voice-dialog-actions`,
`speaker-registry-dialog`, `speaker-list`, `speaker-samples-list`,
`speaker-create-form`, `speaker-picker-dialog`, `speaker-picker-list`,
`speaker-picker-sort`, `upload-toast`, `upload-toast-row`, `upload-toast-title`,
`upload-toast-count`, `upload-toast-file`, `upload-toast-bar`.

## Новые классы и зачем

| Класс | Зачем |
|---|---|
| `app`, `topbar`, `topbar-inner`, `brand*` | у вас шапка была текстовым `<header>` с `h1/p`; в новом виде это компактная панель — своего класса не было |
| `is-on` | единый модификатор «включено» для пилюль, чекбоксов и строк выбора вместо цветовых инлайнов |
| `checkbox`, `count-pip`, `badge` (+`admin/info/warn/accent/sm`) | нарисованные чекбоксы и метки («встроенный», «медиа удалено», «по умолчанию») |
| `popover` (+`menu/right/w-sm/w-md/w-lg`), `popover-title/note/sep`, `menu-item`, `menu-back`, `menu-note`, `menu-foot`, `pop-wrap` | выпадающие меню и селекторы: `btn-menu` описывал только меню задачи |
| `pick-row`, `pick-body`, `pick-label`, `pick-meta`, `pick-sub` | строки выбора с многострочным превью длинных промптов |
| `segmented` (+`square`) | переключатели URL/Файл, фильтр типа, табы диалогов — раньше три разных решения |
| `dialog-backdrop` (+`nested`), `dialog` (+`sm/md/xs`), `dialog-sub` | overlay со скроллом и вложенность (`speaker-picker` над `voice-resolution`) |
| `mgr-columns`, `mgr-col-list`, `mgr-col-form`, `mgr-list`, `mgr-item*` | двухколоночные менеджеры промптов и пресетов |
| `status-pill`, `check-pill`, `task-dot`, `task-break` | статусы задач и результат проверки подключения через классы `status-*` вместо инлайновых цветов |
| `input-shell`, `input-bare`, `text-input`, `field`, `field-head`, `field-label`, `field-meta` | поля ввода без завязки на тип элемента |
| `file-drop*`, `file-list`, `file-row*`, `file-foot*` | мультизагрузка (одна задача из нескольких файлов) |
| `speaker-box`, `speaker-row`, `speaker-select`, `avatar`, `play-btn`, `suggestion*`, `sample-row*`, `merge-row`, `person-row*`, `sample-count`, `speaker-picker-row*`, `speaker-picker-score` | привязка голосов, фрагменты, перенос, вложенный выбор персоны |
| `prompt-artifact*`, `pending-note`, `pending-spinner` | результат промпта как отдельный артефакт |
| `btn-dashed`, `btn-link`, `preset-default-toggle`, `count-badge`, `section`, `section-head`, `card-head`, `card-title`, `page-footer` | мелкие повторяющиеся паттерны |

## Токены

Все значения из прототипа вынесены в переменные: поверхности (`--bg`,
`--bg-card`, `--bg-sunk`, `--bg-inset`, `--bg-field`, `--bg-chip`, `--bg-rail`,
`--bg-bar`), текст (`--ink`, `--ink-soft`, `--ink-mute`, `--ink-faint`,
`--ink-ghost`), линии (`--line`, `--line-strong`, `--line-soft`,
`--line-dashed`), пять семантических групп (`--accent*`, `--ok*`, `--warn*`,
`--danger*`, `--mute*`, `--info*` — каждая с `-ink`, `-soft`, `-line`), радиусы
`--r-xs…--r-3xl` + `--r-pill`, высоты `--h-control`, `--h-control-sm`,
`--h-field`, `--h-topbar`, размеры шрифта `--fs-*`, тени `--sh-*`,
`--focus-ring`, ширина страницы `--page` и `--gutter`.

Ваши старые имена (`--bg`, `--bg-card`, `--ink`, `--ink-soft`, `--line`,
`--accent`, `--accent-2`, `--danger`) сохранены; `--accent-2` переименовать не
пришлось — он живёт как `--ok` в семантической группе, старое имя можно оставить
алиасом: `--accent-2: var(--ok)`.

## Тёмная тема

Только переопределение переменных — правила компонентов не дублируются.
Активируется двумя путями: `html[data-theme="dark"]` (кнопка в шапке) и
`@media (prefers-color-scheme: dark)` для `:root:not([data-theme="light"])`,
то есть системная тема работает, а ручной выбор её перебивает.

Контраст: `--ink-soft` в тёмной теме поднят до `#c3bbab` (7.4:1 на `#191512`),
`--ink-faint` — `#948b7b` (4.6:1), то есть AA держится и для мелкого текста.
Акцент `#c5532a` на тёмном осветлён до `#ef8354` (4.9:1), а текст на акцентной
кнопке инвертирован в `--ink-on-accent: #1a1310` — на светлом фоне кнопки белый
текст давал бы 2.6:1. Фон остался тёплым (`#191512`, `#221d18`), а не серым.

Состояния: `:focus-visible` использует `--focus-ring` (в тёмной теме
полупрозрачный осветлённый акцент), `disabled` — `opacity: .5`, статусы и бейджи
берут свои `-soft`/`-line`/`-ink` тройки, поэтому в тёмной теме они
перекрашиваются автоматически.

## PWA-манифест

Сейчас в манифесте рассогласование: `theme_color: #d65d2b` (старый акцент) и
`background_color: #0f0f10` (почти чёрный при светлом фоне). Правильные значения:

```json
{
  "theme_color": "#c5532a",
  "background_color": "#efeae0"
}
```

Тёмный вариант браузер из манифеста не берёт — его задаёт мета-тег, который
прототип переключает вместе с темой:

```html
<meta name="theme-color" content="#c5532a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#191512" media="(prefers-color-scheme: dark)">
```

Splash-экран (`background_color`) стоит оставить светлым `#efeae0`: он
показывается один раз при запуске, и тёмный вариант манифест не поддерживает.

## Контракт разметки

`id` из `inventory/ids.txt` и ключи `data-i18n*` проставлены на тех элементах,
где прототип покрывает существующий экран (`#url`, `#submit-btn`, `#task-list`,
`#task-filters`, `#filter-q/-type/-from/-to/-clear`, `#language`,
`#prompt-select`, `#delivery-select`, `#preset-select`, `#audio-only-pill`,
`#diarize-pill`, `#speaker-no-manual-stop-pill`, диалоги и их
`*-close-btn`/`*-list`/`*-form` и т.д.). Там, где кнопка содержит иконку и
подпись, `data-i18n` висит на внутреннем `<span>` — иконка при переводе не
затирается.

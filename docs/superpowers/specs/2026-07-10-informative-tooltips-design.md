# Информативные tooltip'ы на кнопках (VOS-73 / vts-3rb)

**Дата:** 2026-07-10
**Linear:** https://linear.app/vostrikov/issue/VOS-73/sdelat-vsplyvayushie-podskazki-na-knopkah-bolee-informativnymi
**bd:** vts-3rb

## Цель

Пересмотреть tooltip'ы кнопок в веб-интерфейсе VTS: пользователю должно быть понятно,
что именно сделает кнопка; для необратимых/неоднозначных действий подсказка объясняет
результат. Текст короткий, но не телеграфный. Одинаковые действия — единообразные
формулировки. Локали: ru / en / de.

## Принятые решения

- **Стиль — дифференцированный.** Простые однозначные действия: «глагол + объект»
  («Обновить список задач»). Опасные/неоднозначные: короткое пояснение результата
  («Удалить задачу со всеми файлами — безвозвратно»). Формулировки — в терминах
  пользователя, без внутренней терминологии (никаких «reset summary stages»).
- **Объём — тексты + пробелы.** Переписываем существующие i18n-ключи и добавляем
  подсказки только тем кнопкам, где текст кнопки не объясняет результат. Кастомные
  CSS-tooltip'ы — вне объёма (отдельная задача, если понадобится).
- **Терминология:** «сводка» (не «summary») в RU; Zusammenfassung в DE. Объект
  всегда назван: «Удалить промпт», «Удалить пресет», не голое «Удалить».

## Семантика опасных действий (проверена по бэкенду)

- **Удаление** (`DELETE /api/tasks`, main.py): отменяет выполнение, удаляет запись
  и весь каталог артефактов (транскрипт, сводка, медиа, логи). Необратимо.
- **Архивирование** (`POST /api/tasks/archive`): только completed/failed; удаляет
  медиа и промежуточные файлы, сохраняет транскрипт, сводку и лог; статус → archived
  (задача уходит из списка).
- **Пауза**: запрос на остановку; процессор останавливается после текущего шага.
  «Возобновить» продолжает с места остановки.
- **Перезапуск сводки**: режим `full` пересчитывает сводку по всем частям заново;
  `final_only` пересобирает только итоговую сводку из готовых частей.

## Изменения текстов существующих ключей

### Опасные / неоднозначные — с пояснением результата

| Ключ | RU | EN | DE |
|---|---|---|---|
| `action.delete` | Удалить задачу со всеми файлами — безвозвратно | Delete the task and all its files — cannot be undone | Aufgabe mit allen Dateien löschen – kann nicht rückgängig gemacht werden |
| `action.archive` | Архивировать: убрать из списка и удалить медиа; транскрипт и сводка сохранятся | Archive: remove from the list and delete media; transcript and summary are kept | Archivieren: aus der Liste entfernen und Medien löschen; Transkript und Zusammenfassung bleiben erhalten |
| `action.pause` | Приостановить обработку после текущего шага | Pause processing after the current step | Verarbeitung nach dem aktuellen Schritt anhalten |
| `action.resume` | Продолжить обработку с места остановки | Resume processing from where it stopped | Verarbeitung an der angehaltenen Stelle fortsetzen |
| `action.restart_summary` | Перезапустить сводку… | Restart summary… | Zusammenfassung neu erstellen… |
| `admin.switch_user` | Выбрать пользователя, от имени которого работать | Choose a user to act as | Benutzer auswählen, als der gearbeitet wird |
| `admin.apply` | Работать от имени выбранного пользователя | Act as the selected user | Als ausgewählter Benutzer arbeiten |
| `admin.use_self` | Вернуться к работе от своего имени | Switch back to your own account | Zum eigenen Benutzer zurückkehren |
| `action.copy_tab` | Скопировать содержимое открытой вкладки в буфер обмена | Copy the open tab's content to the clipboard | Inhalt des geöffneten Tabs in die Zwischenablage kopieren |
| `action.save_tab` | Скачать содержимое открытой вкладки файлом | Download the open tab's content as a file | Inhalt des geöffneten Tabs als Datei herunterladen |
| `action.download_media` | Скачать исходный медиафайл | Download the original media file | Originale Mediendatei herunterladen |
| `action.enable_notifications` | Включить браузерные уведомления о завершении задач | Enable browser notifications when tasks finish | Browser-Benachrichtigungen bei Aufgabenabschluss aktivieren |
| `preset.manage.make_default` | Использовать этот пресет по умолчанию для новых задач | Use this preset by default for new tasks | Dieses Preset als Standard für neue Aufgaben verwenden |
| `about.open` | Показать параметры и детали задачи | Show task settings and details | Einstellungen und Details der Aufgabe anzeigen |
| `tokens.open` | Управление API-токенами | Manage API tokens | API-Tokens verwalten |

Многоточие в `action.restart_summary` — сигнал, что кнопка открывает меню выбора.

### Простые — уточнить объект

| Ключ | RU | EN | DE |
|---|---|---|---|
| `action.refresh` | Обновить список задач | Refresh the task list | Aufgabenliste aktualisieren |
| `action.expand` | Развернуть подробности задачи | Expand task details | Aufgabendetails ausklappen |
| `action.collapse` | Свернуть подробности задачи | Collapse task details | Aufgabendetails einklappen |
| `action.edit_name` | Переименовать задачу | Rename task | Aufgabe umbenennen |
| `action.save_name` | Сохранить новое имя | Save the new name | Neuen Namen speichern |
| `action.cancel_edit` | Отменить переименование | Cancel renaming | Umbenennen abbrechen |
| `action.logout` | Выйти из аккаунта | Log out | Abmelden |
| `prompts.manage.edit` | Изменить промпт | Edit prompt | Prompt bearbeiten |
| `prompts.manage.delete` | Удалить промпт | Delete prompt | Prompt löschen |
| `prompts.manage.duplicate` | Дублировать промпт | Duplicate prompt | Prompt duplizieren |
| `preset.manage.edit` | Изменить пресет | Edit preset | Preset bearbeiten |
| `preset.manage.delete` | Удалить пресет | Delete preset | Preset löschen |
| `preset.manage.duplicate` | Дублировать пресет | Duplicate preset | Preset duplizieren |

**Побочный эффект (намеренный):** `prompts.manage.edit` и `preset.manage.edit`
используются также как подпись submit-кнопки в форме редактирования
(app.js:3157, app.js:3354, index.html:607) — подпись станет «Изменить промпт» /
«Изменить пресет». Это точнее текущего голого «Изменить».

### Без изменений

`action.create`, `tokens.close`, `tab.*` (метки вкладок и `*_pending`-варианты уже
информативны; ключи двойного назначения — метка и tooltip),
`tasks.media_expired_tooltip`, `prompts.manage.open`, `preset.manage.open`.

## Новые подсказки (новые ключи)

Только там, где текст кнопки не объясняет результат:

| Новый ключ | Элемент | RU | EN | DE |
|---|---|---|---|---|
| `preset.save_as_tooltip` | `#preset-save-btn` | Сохранить текущие настройки задачи как новый пресет | Save the current task settings as a new preset | Aktuelle Aufgabeneinstellungen als neues Preset speichern |
| `preset.resave_tooltip` | `#preset-resave-btn` | Записать текущие настройки в выбранный пресет | Overwrite the selected preset with the current settings | Ausgewähltes Preset mit den aktuellen Einstellungen überschreiben |
| `action.restart_summary_full_tooltip` | `.restart-summary-full-btn` | Пересчитать сводку заново по всем частям транскрипта | Recompute the summary from scratch over the whole transcript | Zusammenfassung komplett aus dem gesamten Transkript neu berechnen |
| `action.restart_summary_final_tooltip` | `.restart-summary-final-btn` | Пересобрать только итоговую сводку из уже готовых частей | Rebuild only the final summary from already processed parts | Nur die finale Zusammenfassung aus bereits verarbeiteten Teilen neu erstellen |

## Затрагиваемые файлы

- `vts/static/i18n/ru.js`, `en.js`, `de.js` — изменённые и новые ключи.
- `vts/static/index.html` — `data-i18n-title` для `#preset-save-btn` /
  `#preset-resave-btn`; синхронизация статичных английских `title=`-fallback'ов
  с новыми en-значениями.
- `vts/static/app.js` — две строки: `title` для пунктов меню перезапуска сводки
  рядом с существующими присвоениями `textContent` (строки ~1650–1653). Другой
  логики JS не касаемся.

## Проверка

1. Скрипт-сверка: каждый ключ из `data-i18n-title` (index.html) и `t("…")`-титулов
   (app.js) присутствует во всех трёх словарях; статичные `title=`-fallback'ы в
   index.html совпадают с en.js.
2. Скилл `verifier-web`: подсказки применяются на ru/en/de (выборочно: delete,
   archive, pause, admin apply, пункты меню перезапуска, save-as-preset).
3. Бамп версии в `vts/__init__.py`, коммит, push. Build-тег — только по явной
   команде.

## Вне объёма

- Кастомные CSS-tooltip'ы (задержка, перенос строк, тач-устройства).
- Изменение текстов подтверждающих диалогов (`confirm.*`) — они уже описывают
  результат; при рассинхроне формулировок с новыми tooltip'ами — точечная правка
  в рамках имплементации.

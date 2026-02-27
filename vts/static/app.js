const taskList = document.getElementById("task-list");
const taskTemplate = document.getElementById("task-template");
const form = document.getElementById("task-form");
const authUserLabel = document.getElementById("auth-user");
const adminControls = document.getElementById("admin-controls");
const adminSelect = document.getElementById("admin-user-select");
const adminApplyBtn = document.getElementById("admin-apply-btn");
const adminResetBtn = document.getElementById("admin-reset-btn");
const appVersionLabel = document.getElementById("app-version");
const refreshBtn = document.getElementById("refresh-btn");
const BUILD_VERSION = String(window.__VTS_BUILD_VERSION__ || "0.0.0");
const VERSION_CHECK_INTERVAL_MS = 30000;
const QUEUE_POLL_INTERVAL_MS = 5000;

const DAG_STEPS = [
  "download",
  "extract_audio",
  "segment_audio",
  "transcribe_segments",
  "merge_transcript",
  "prepare_llama_model",
  "prepare_summary_chunks",
  "summarize_windows",
  "summarize_final"
];
const SUMMARY_STEPS = new Set([
  "prepare_llama_model",
  "prepare_summary_chunks",
  "summarize_windows",
  "summarize_final"
]);

const I18N = {
  en: {
    "header.title": "Video Transcribe & Summarize",
    "header.subtitle": "Queue URL, monitor pipeline, inspect transcript and summary artifacts.",
    "header.version": "Version:",
    "context.authenticated": "Authenticated:",
    "context.acting_as": "Working as:",
    "context.admin_suffix": " (admin)",
    "admin.title": "Admin Panel",
    "admin.switch_user": "Switch to user",
    "admin.apply": "Apply",
    "admin.use_self": "Use self",
    "new_task.title": "New Task",
    "new_task.url_label": "Video URL",
    "new_task.url_placeholder": "https://youtube.com/watch?v=...",
    "new_task.audio_only": "Audio only",
    "new_task.transcript": "Transcript",
    "new_task.summary": "Summary",
    "new_task.language": "Language",
    "new_task.language_auto": "auto",
    "new_task.language_en": "English",
    "new_task.language_ru": "Russian",
    "new_task.language_de": "German",
    "new_task.language_fr": "French",
    "new_task.language_es": "Spanish",
    "tasks.title": "Tasks",
    "action.create": "Create task",
    "action.refresh": "Refresh tasks",
    "action.pause": "Pause",
    "action.resume": "Resume",
    "action.delete": "Delete",
    "action.expand": "Expand",
    "action.collapse": "Collapse",
    "tab.transcript": "Transcript",
    "tab.summary": "Summary",
    "tab.log": "Log",
    "tab.prompt_transcript": "Select tab to load transcript",
    "tab.prompt_summary": "Select tab to load summary",
    "tab.prompt_log": "Select tab to load task log",
    "status.running": "running",
    "status.queued": "queued",
    "status.paused": "paused",
    "status.completed": "completed",
    "status.failed": "failed",
    "status.canceled": "canceled",
    "status.queued_pos": "queued #{position}",
    "step.line": "Step {index} of {total}: {step}",
    "step.waiting": "Step - of {total}: waiting",
    "progress.working": "in progress",
    "progress.queued": "queued",
    "progress.queue_pos": "queue #{position}",
    "progress.failed": "failed",
    "confirm.delete": "Delete task? This action cannot be undone.",
    "steps.download": "Media download",
    "steps.extract_audio": "Audio extraction",
    "steps.segment_audio": "Audio segmentation",
    "steps.transcribe_segments": "Segment transcription",
    "steps.merge_transcript": "Transcript merge",
    "steps.prepare_llama_model": "LLM warm-up",
    "steps.prepare_summary_chunks": "Summary chunking",
    "steps.summarize_windows": "Window summaries",
    "steps.summarize_final": "Final summary"
  },
  ru: {
    "header.title": "Транскрибация и суммаризация видео",
    "header.subtitle": "Поставьте URL в очередь, следите за пайплайном и проверяйте артефакты транскрипта и summary.",
    "header.version": "Версия:",
    "context.authenticated": "Аутентифицирован:",
    "context.acting_as": "Работаю как:",
    "context.admin_suffix": " (админ)",
    "admin.title": "Панель администратора",
    "admin.switch_user": "Переключиться на пользователя",
    "admin.apply": "Применить",
    "admin.use_self": "Свой пользователь",
    "new_task.title": "Новая задача",
    "new_task.url_label": "URL видео",
    "new_task.url_placeholder": "https://youtube.com/watch?v=...",
    "new_task.audio_only": "Только аудио",
    "new_task.transcript": "Транскрипт",
    "new_task.summary": "Сводка",
    "new_task.language": "Язык",
    "new_task.language_auto": "авто",
    "new_task.language_en": "Английский",
    "new_task.language_ru": "Русский",
    "new_task.language_de": "Немецкий",
    "new_task.language_fr": "Французский",
    "new_task.language_es": "Испанский",
    "tasks.title": "Задачи",
    "action.create": "Создать задачу",
    "action.refresh": "Обновить задачи",
    "action.pause": "Пауза",
    "action.resume": "Возобновить",
    "action.delete": "Удалить",
    "action.expand": "Развернуть",
    "action.collapse": "Свернуть",
    "tab.transcript": "Транскрипт",
    "tab.summary": "Сводка",
    "tab.log": "Лог",
    "tab.prompt_transcript": "Выберите вкладку, чтобы загрузить транскрипт",
    "tab.prompt_summary": "Выберите вкладку, чтобы загрузить сводку",
    "tab.prompt_log": "Выберите вкладку, чтобы загрузить лог задачи",
    "status.running": "выполняется",
    "status.queued": "в очереди",
    "status.paused": "пауза",
    "status.completed": "завершено",
    "status.failed": "ошибка",
    "status.canceled": "отменено",
    "status.queued_pos": "очередь #{position}",
    "step.line": "Шаг {index} из {total}: {step}",
    "step.waiting": "Шаг - из {total}: ожидание",
    "progress.working": "идет работа",
    "progress.queued": "в очереди",
    "progress.queue_pos": "очередь #{position}",
    "progress.failed": "ошибка",
    "confirm.delete": "Удалить задачу? Это действие необратимо.",
    "steps.download": "Загрузка медиа",
    "steps.extract_audio": "Извлечение аудио",
    "steps.segment_audio": "Сегментация аудио",
    "steps.transcribe_segments": "Транскрибация сегментов",
    "steps.merge_transcript": "Сборка транскрипта",
    "steps.prepare_llama_model": "Подготовка LLM",
    "steps.prepare_summary_chunks": "Подготовка окон summary",
    "steps.summarize_windows": "Сводка по окнам",
    "steps.summarize_final": "Финальная сводка"
  },
  de: {
    "header.title": "Video transkribieren und zusammenfassen",
    "header.subtitle": "URL in die Warteschlange stellen, Pipeline beobachten und Transkript-/Summary-Artefakte prüfen.",
    "header.version": "Version:",
    "context.authenticated": "Authentifiziert:",
    "context.acting_as": "Arbeitet als:",
    "context.admin_suffix": " (Admin)",
    "admin.title": "Admin-Bereich",
    "admin.switch_user": "Zu Benutzer wechseln",
    "admin.apply": "Anwenden",
    "admin.use_self": "Eigener Benutzer",
    "new_task.title": "Neue Aufgabe",
    "new_task.url_label": "Video-URL",
    "new_task.url_placeholder": "https://youtube.com/watch?v=...",
    "new_task.audio_only": "Nur Audio",
    "new_task.transcript": "Transkript",
    "new_task.summary": "Zusammenfassung",
    "new_task.language": "Sprache",
    "new_task.language_auto": "auto",
    "new_task.language_en": "Englisch",
    "new_task.language_ru": "Russisch",
    "new_task.language_de": "Deutsch",
    "new_task.language_fr": "Französisch",
    "new_task.language_es": "Spanisch",
    "tasks.title": "Aufgaben",
    "action.create": "Aufgabe erstellen",
    "action.refresh": "Aufgaben aktualisieren",
    "action.pause": "Pausieren",
    "action.resume": "Fortsetzen",
    "action.delete": "Löschen",
    "action.expand": "Erweitern",
    "action.collapse": "Einklappen",
    "tab.transcript": "Transkript",
    "tab.summary": "Zusammenfassung",
    "tab.log": "Log",
    "tab.prompt_transcript": "Tab auswählen, um das Transkript zu laden",
    "tab.prompt_summary": "Tab auswählen, um die Zusammenfassung zu laden",
    "tab.prompt_log": "Tab auswählen, um das Aufgaben-Log zu laden",
    "status.running": "läuft",
    "status.queued": "in warteschlange",
    "status.paused": "pausiert",
    "status.completed": "abgeschlossen",
    "status.failed": "fehlgeschlagen",
    "status.canceled": "abgebrochen",
    "status.queued_pos": "warteschlange #{position}",
    "step.line": "Schritt {index} von {total}: {step}",
    "step.waiting": "Schritt - von {total}: warten",
    "progress.working": "in bearbeitung",
    "progress.queued": "in warteschlange",
    "progress.queue_pos": "warteschlange #{position}",
    "progress.failed": "fehlgeschlagen",
    "confirm.delete": "Aufgabe löschen? Diese Aktion kann nicht rückgängig gemacht werden.",
    "steps.download": "Medien-Download",
    "steps.extract_audio": "Audio-Extraktion",
    "steps.segment_audio": "Audio-Segmentierung",
    "steps.transcribe_segments": "Segment-Transkription",
    "steps.merge_transcript": "Transkript-Zusammenführung",
    "steps.prepare_llama_model": "LLM-Aufwärmen",
    "steps.prepare_summary_chunks": "Summary-Chunking",
    "steps.summarize_windows": "Fenster-Zusammenfassungen",
    "steps.summarize_final": "Finale Zusammenfassung"
  }
};

function detectLocale() {
  const candidates = [];
  if (typeof navigator !== "undefined" && Array.isArray(navigator.languages)) {
    candidates.push(...navigator.languages);
  }
  if (typeof navigator !== "undefined" && navigator.language) {
    candidates.push(navigator.language);
  }
  for (const candidate of candidates) {
    const normalized = String(candidate || "").toLowerCase();
    if (!normalized) {
      continue;
    }
    const short = normalized.split(/[-_]/)[0];
    if (I18N[short]) {
      return short;
    }
  }
  return "en";
}

const state = {
  locale: detectLocale(),
  authUser: localStorage.getItem("vts_auth_user") || "demo@example.com",
  actingAs: localStorage.getItem("vts_as_user") || "",
  me: null,
  eventSource: null,
  versionTimer: null,
  durationTimer: null,
  queueTimer: null,
  queueRefreshInFlight: false
};

function interpolate(template, params = {}) {
  return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, (full, key) => {
    const value = params[key];
    return value === undefined || value === null ? full : String(value);
  });
}

function t(key, params = {}) {
  const localeDict = I18N[state.locale] || I18N.en;
  const raw = localeDict[key] ?? I18N.en[key] ?? key;
  return interpolate(raw, params);
}

function statusText(status) {
  const key = `status.${status}`;
  const translated = t(key);
  return translated === key ? String(status || "") : translated;
}

function stepText(stepName) {
  const key = `steps.${stepName}`;
  const translated = t(key);
  return translated === key ? String(stepName || "") : translated;
}

function applyI18n(root = document) {
  const scope = root || document;
  const applyAttr = (attr, updater) => {
    if (scope instanceof Element && scope.hasAttribute(attr)) {
      updater(scope);
    }
    scope.querySelectorAll(`[${attr}]`).forEach((el) => updater(el));
  };
  applyAttr("data-i18n", (el) => {
    el.textContent = t(el.getAttribute("data-i18n") || "");
  });
  applyAttr("data-i18n-placeholder", (el) => {
    el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder") || ""));
  });
  applyAttr("data-i18n-title", (el) => {
    el.setAttribute("title", t(el.getAttribute("data-i18n-title") || ""));
  });
  applyAttr("data-i18n-aria-label", (el) => {
    el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria-label") || ""));
  });
}

function applyI18nToPage() {
  document.documentElement.lang = state.locale;
  applyI18n(document);
}

function setVersionLabel(version) {
  if (!appVersionLabel) {
    return;
  }
  const value = String(version || "").trim();
  appVersionLabel.textContent = value ? `v${value}` : "-";
}

function formatDuration(seconds) {
  const safe = Math.max(0, Math.floor(seconds));
  const h = Math.floor(safe / 3600);
  const m = Math.floor((safe % 3600) / 60);
  const s = safe % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseIsoMs(value) {
  if (!value) {
    return null;
  }
  const ts = Date.parse(value);
  return Number.isFinite(ts) ? ts : null;
}

function buildPath(path) {
  const url = new URL(path, window.location.origin);
  if (state.actingAs) {
    url.searchParams.set("as_user", state.actingAs);
  }
  return url.pathname + url.search;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  headers["X-Forwarded-User"] = state.authUser;
  const response = await fetch(buildPath(path), { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function forceReloadToVersion(version) {
  const target = new URL("/", window.location.origin);
  target.searchParams.set("v", version);
  target.searchParams.set("ts", String(Date.now()));
  window.location.replace(target.toString());
}

async function checkServerVersion() {
  try {
    const response = await fetch(`/api/version?ts=${Date.now()}`, {
      method: "GET",
      cache: "no-store"
    });
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    const serverVersion = String(payload.version || "");
    setVersionLabel(serverVersion || BUILD_VERSION);
    if (serverVersion && serverVersion !== BUILD_VERSION) {
      forceReloadToVersion(serverVersion);
    }
  } catch {
    // Ignore transient network errors.
  }
}

function startVersionWatcher() {
  if (state.versionTimer) {
    window.clearInterval(state.versionTimer);
  }
  state.versionTimer = window.setInterval(checkServerVersion, VERSION_CHECK_INTERVAL_MS);
}

function startDurationTicker() {
  if (state.durationTimer) {
    window.clearInterval(state.durationTimer);
  }
  state.durationTimer = window.setInterval(() => {
    document.querySelectorAll(".task").forEach((taskEl) => renderTaskRuntime(taskEl));
  }, 1000);
}

function isLocalDevHost() {
  const host = window.location.hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function getEnabledSteps(task) {
  const options = task.options || {};
  const transcriptEnabled = options.transcript !== false;
  const summaryEnabled = options.summary !== false;
  if (!transcriptEnabled) {
    return ["download"];
  }
  if (!summaryEnabled) {
    return DAG_STEPS.filter((step) => !SUMMARY_STEPS.has(step));
  }
  return [...DAG_STEPS];
}

function findStep(task, wantedStatus) {
  return (task.steps || []).find((step) => step.status === wantedStatus) || null;
}

function computeTaskStartedAt(task) {
  const startedTimes = (task.steps || []).map((step) => parseIsoMs(step.started_at)).filter((value) => value !== null);
  if (startedTimes.length === 0) {
    return null;
  }
  return Math.min(...startedTimes);
}

function normalizeProgress(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Math.max(0, Math.min(1, numeric));
}

function parseQueuePosition(value) {
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric > 0 ? numeric : null;
}

function createRuntime(task) {
  const runningStep = findStep(task, "running");
  const failedStep = findStep(task, "failed");
  return {
    sourceUrl: String(task.source_url || ""),
    displayName: "",
    baseStatus: String(task.status || ""),
    queuePosition: parseQueuePosition(task.queue_position),
    enabledSteps: getEnabledSteps(task),
    currentStepName: runningStep ? runningStep.name : failedStep ? failedStep.name : "",
    failedStepName: failedStep ? failedStep.name : "",
    currentStepStartedAt: runningStep ? parseIsoMs(runningStep.started_at) : null,
    taskStartedAt: computeTaskStartedAt(task),
    mediaPhase: "",
    llamaStatus: "idle",
    download: {
      phase: "",
      video: 0,
      audio: 0,
      hasVideo: false,
      hasAudio: false
    },
    transcribe: {
      current: 0,
      total: 0
    },
    summary: {
      current: 0,
      total: 0
    }
  };
}

function resolveActiveStep(runtime) {
  if (runtime.currentStepName) {
    return runtime.currentStepName;
  }
  if (runtime.failedStepName) {
    return runtime.failedStepName;
  }
  if (runtime.baseStatus === "queued" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[0];
  }
  if (runtime.baseStatus === "completed" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[runtime.enabledSteps.length - 1];
  }
  return "";
}

function computeStepProgress(runtime) {
  if (runtime.baseStatus === "completed") {
    return { value: 1, indeterminate: false, text: "100%" };
  }
  if (runtime.baseStatus === "failed") {
    return { value: 1, indeterminate: false, text: t("progress.failed") };
  }
  if (runtime.baseStatus === "queued") {
    if (runtime.queuePosition) {
      return { value: 0, indeterminate: false, text: t("progress.queue_pos", { position: runtime.queuePosition }) };
    }
    return { value: 0, indeterminate: false, text: t("progress.queued") };
  }

  const active = resolveActiveStep(runtime);
  let value = 0;
  let indeterminate = false;
  let textOverride = "";

  if (active === "download") {
    const phase = runtime.download.phase;
    if (runtime.mediaPhase === "merge" || runtime.mediaPhase === "postprocess") {
      value = 0.92;
      indeterminate = true;
    } else if (runtime.download.hasVideo && runtime.download.hasAudio) {
      if (phase === "video") {
        value = runtime.download.video * 0.5;
      } else if (phase === "audio") {
        value = 0.5 + runtime.download.audio * 0.5;
      } else {
        value = Math.max(runtime.download.video * 0.5, 0.5 + runtime.download.audio * 0.5);
      }
    } else if (runtime.download.hasVideo) {
      value = runtime.download.video * 0.5;
    } else if (runtime.download.hasAudio) {
      value = runtime.download.audio;
    } else {
      indeterminate = true;
    }
  } else if (active === "transcribe_segments") {
    if (runtime.transcribe.total > 0) {
      value = normalizeProgress(runtime.transcribe.current / runtime.transcribe.total);
    } else {
      indeterminate = true;
    }
  } else if (active === "summarize_windows") {
    if (runtime.summary.total > 0) {
      const current = Math.max(0, Math.min(runtime.summary.current, runtime.summary.total));
      value = normalizeProgress(current / runtime.summary.total);
      textOverride = `${current}/${runtime.summary.total}`;
    } else {
      indeterminate = true;
    }
  } else if (active === "summarize_final") {
    if (runtime.summary.total > 0) {
      const current = Math.max(0, Math.min(runtime.summary.current, runtime.summary.total));
      value = normalizeProgress(current / runtime.summary.total);
      textOverride = `${current}/${runtime.summary.total}`;
    } else {
      indeterminate = true;
    }
  } else if (active === "prepare_llama_model") {
    if (runtime.llamaStatus === "ready") {
      value = 1;
    } else {
      indeterminate = true;
    }
  } else {
    indeterminate = true;
  }

  if (indeterminate) {
    return { value: Math.max(0.05, value), indeterminate: true, text: t("progress.working") };
  }
  if (textOverride) {
    return { value, indeterminate: false, text: textOverride };
  }
  return { value, indeterminate: false, text: `${Math.round(value * 100)}%` };
}

function setTaskStatusAppearance(statusEl, status, queuePosition = null) {
  if (status === "queued" && queuePosition) {
    statusEl.textContent = t("status.queued_pos", { position: queuePosition });
  } else {
    statusEl.textContent = statusText(status);
  }
  statusEl.className = "task-status";
  statusEl.classList.add(`status-${status}`);
}

function renderTaskTitle(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  const hasName = Boolean(runtime.displayName);
  elements.linkEl.textContent = hasName ? runtime.displayName : runtime.sourceUrl;
  elements.linkEl.href = runtime.sourceUrl;
  elements.sourceEl.textContent = runtime.sourceUrl;
  elements.sourceEl.classList.toggle("hidden", !hasName);
}

function renderTaskRuntime(taskEl) {
  if (!taskEl || !taskEl._runtime || !taskEl._elements) {
    return;
  }
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;

  renderTaskTitle(taskEl);
  setTaskStatusAppearance(elements.statusEl, runtime.baseStatus, runtime.queuePosition);
  const canPause = runtime.baseStatus === "queued" || runtime.baseStatus === "running";
  const canResume = runtime.baseStatus === "paused";
  elements.pauseBtn.disabled = !canPause;
  elements.resumeBtn.disabled = !canResume;

  if (runtime.baseStatus === "running") {
    if (!runtime.taskStartedAt) {
      runtime.taskStartedAt = Date.now();
    }
    const elapsed = (Date.now() - runtime.taskStartedAt) / 1000;
    elements.taskRuntimeEl.textContent = formatDuration(elapsed);
  } else {
    elements.taskRuntimeEl.textContent = "";
  }

  const activeStep = resolveActiveStep(runtime);
  const stepIndex = runtime.enabledSteps.indexOf(activeStep) + 1;
  const normalizedIndex = Math.max(stepIndex, 1);
  if (activeStep) {
    elements.stepLabelEl.textContent = t("step.line", {
      index: normalizedIndex,
      total: runtime.enabledSteps.length,
      step: stepText(activeStep)
    });
  } else {
    elements.stepLabelEl.textContent = t("step.waiting", { total: runtime.enabledSteps.length });
  }

  if (runtime.baseStatus === "running" && runtime.currentStepStartedAt) {
    const elapsed = (Date.now() - runtime.currentStepStartedAt) / 1000;
    elements.stepTimeEl.textContent = formatDuration(elapsed);
  } else {
    elements.stepTimeEl.textContent = "-";
  }

  const progress = computeStepProgress(runtime);
  elements.progressWrap.classList.toggle("indeterminate", progress.indeterminate);
  elements.progressFill.style.width = `${Math.round(progress.value * 100)}%`;
  elements.progressText.textContent = progress.text;
  elements.progressWrap.setAttribute("aria-valuenow", String(Math.round(progress.value * 100)));
}

function renderTasks(tasks) {
  taskList.innerHTML = "";
  tasks.forEach((task) => {
    const node = taskTemplate.content.cloneNode(true);
    const root = node.querySelector(".task");
    const body = node.querySelector(".task-body");
    const toggleBtn = root.querySelector(".toggle-btn");
    const pauseBtn = root.querySelector(".pause-btn");
    const resumeBtn = root.querySelector(".resume-btn");
    const deleteBtn = root.querySelector(".delete-btn");
    const transcriptPre = root.querySelector(".tab-content.transcript");
    const summaryPre = root.querySelector(".tab-content.summary");
    const logPre = root.querySelector(".tab-content.log");

    applyI18n(root);

    root.dataset.taskId = task.id;
    transcriptPre.textContent = t("tab.prompt_transcript");
    summaryPre.textContent = t("tab.prompt_summary");
    logPre.textContent = t("tab.prompt_log");

    pauseBtn.title = t("action.pause");
    pauseBtn.setAttribute("aria-label", t("action.pause"));
    resumeBtn.title = t("action.resume");
    resumeBtn.setAttribute("aria-label", t("action.resume"));
    deleteBtn.title = t("action.delete");
    deleteBtn.setAttribute("aria-label", t("action.delete"));
    toggleBtn.title = t("action.expand");
    toggleBtn.setAttribute("aria-label", t("action.expand"));

    root.querySelectorAll(".tab-btn").forEach((btn) => {
      const tabName = String(btn.dataset.tab || "");
      const tabLabel = t(`tab.${tabName}`);
      btn.textContent = tabLabel === `tab.${tabName}` ? tabName : tabLabel;
    });

    toggleBtn.addEventListener("click", () => {
      body.classList.toggle("hidden");
      const expanded = !body.classList.contains("hidden");
      toggleBtn.classList.toggle("expanded", expanded);
      const label = expanded ? t("action.collapse") : t("action.expand");
      toggleBtn.title = label;
      toggleBtn.setAttribute("aria-label", label);
    });
    pauseBtn.addEventListener("click", () => updateTaskStatus(task.id, "pause"));
    resumeBtn.addEventListener("click", () => updateTaskStatus(task.id, "resume"));
    deleteBtn.addEventListener("click", () => removeTask(task.id));

    root.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        root.querySelectorAll(".tab-btn").forEach((item) => item.classList.remove("active"));
        root.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
        btn.classList.add("active");
        const tab = btn.dataset.tab;
        const panel = root.querySelector(`.tab-content.${tab}`);
        if (!panel) {
          return;
        }
        panel.classList.add("active");
        if (tab === "transcript") {
          transcriptPre.textContent = await api(`/api/tasks/${task.id}/transcript`).catch((err) => err.message);
        } else if (tab === "summary") {
          summaryPre.textContent = await api(`/api/tasks/${task.id}/summary`).catch((err) => err.message);
        } else if (tab === "log") {
          logPre.textContent = await api(`/api/tasks/${task.id}/log`).catch((err) => err.message);
        }
      });
    });

    root._elements = {
      linkEl: root.querySelector(".task-link"),
      sourceEl: root.querySelector(".task-source"),
      statusEl: root.querySelector(".task-status"),
      taskRuntimeEl: root.querySelector(".task-runtime"),
      pauseBtn,
      resumeBtn,
      stepLabelEl: root.querySelector(".step-label"),
      stepTimeEl: root.querySelector(".step-time"),
      progressWrap: root.querySelector(".step-progress"),
      progressFill: root.querySelector(".step-progress-fill"),
      progressText: root.querySelector(".step-progress-text")
    };
    root._runtime = createRuntime(task);
    renderTaskRuntime(root);
    taskList.appendChild(node);
  });
  updateQueueWatcher(tasks);
}

async function loadTasks() {
  const tasks = await api("/api/tasks").catch((err) => {
    taskList.textContent = err.message;
    return [];
  });
  renderTasks(tasks);
}

async function createTask(event) {
  event.preventDefault();
  const payload = {
    url: form.url.value,
    language: form.language.value || null,
    audio_only: form.audio_only.checked,
    transcript: form.transcript.checked,
    summary: form.summary.checked
  };
  await api("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  form.reset();
  form.transcript.checked = true;
  form.summary.checked = true;
  syncSummaryToggle();
  await loadTasks();
}

function syncSummaryToggle() {
  if (!form.transcript.checked) {
    form.summary.checked = false;
    form.summary.disabled = true;
    return;
  }
  form.summary.disabled = false;
}

async function updateTaskStatus(taskId, action) {
  await api(`/api/tasks/${taskId}/${action}`, { method: "POST" });
  await loadTasks();
}

async function removeTask(taskId) {
  const confirmed = window.confirm(t("confirm.delete"));
  if (!confirmed) {
    return;
  }
  await api(`/api/tasks/${taskId}`, { method: "DELETE" });
  await loadTasks();
}

function findTaskEl(taskId) {
  return document.querySelector(`[data-task-id="${taskId}"]`);
}

function patchTaskStatus(taskId, status) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.baseStatus = String(status || "");
  if (runtime.baseStatus !== "queued") {
    runtime.queuePosition = null;
  }
  if (runtime.baseStatus === "running" && !runtime.taskStartedAt) {
    runtime.taskStartedAt = Date.now();
  }
  renderTaskRuntime(taskEl);
  updateQueueWatcherFromDom();
  if (runtime.baseStatus === "queued") {
    void refreshQueuePositions();
  }
}

function patchTaskStep(taskId, name, status) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  const stepName = String(name || "");
  const stepStatus = String(status || "");
  if (stepStatus === "running") {
    runtime.currentStepName = stepName;
    runtime.failedStepName = "";
    runtime.currentStepStartedAt = Date.now();
    if (!runtime.taskStartedAt) {
      runtime.taskStartedAt = Date.now();
    }
  } else if (stepStatus === "failed") {
    runtime.currentStepName = stepName;
    runtime.failedStepName = stepName;
  }
  renderTaskRuntime(taskEl);
}

function patchTaskProgress(taskId, phase, payload) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  const stepPhase = String(phase || "");
  runtime.download.phase = stepPhase;
  if (stepPhase === "video") {
    runtime.download.video = normalizeProgress(payload.progress);
    runtime.download.hasVideo = true;
  } else if (stepPhase === "audio") {
    runtime.download.audio = normalizeProgress(payload.progress);
    runtime.download.hasAudio = true;
  }
  const mediaTitle = typeof payload.media_title === "string" ? payload.media_title.trim() : "";
  const mediaFilename = typeof payload.media_filename === "string" ? payload.media_filename.trim() : "";
  if (mediaTitle) {
    runtime.displayName = mediaTitle;
  } else if (mediaFilename && !runtime.displayName) {
    runtime.displayName = mediaFilename;
  }
  renderTaskRuntime(taskEl);
}

function patchTaskPhase(taskId, phase, status) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  const phaseName = String(phase || "").toLowerCase();
  const phaseStatus = String(status || "").toLowerCase();
  runtime.mediaPhase = phaseStatus === "running" ? phaseName : "";
  renderTaskRuntime(taskEl);
}

function patchLlamaModelProgress(taskId, status) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.llamaStatus = status === "loading" ? "loading" : status === "ready" ? "ready" : "idle";
  if (runtime.llamaStatus === "loading" && !runtime.currentStepName) {
    runtime.currentStepName = "prepare_llama_model";
    runtime.currentStepStartedAt = Date.now();
  }
  renderTaskRuntime(taskEl);
}

function patchTranscribeProgress(taskId, current, total) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.transcribe.current = Number(current) || 0;
  runtime.transcribe.total = Number(total) || 0;
  renderTaskRuntime(taskEl);
}

function patchSummaryProgress(taskId, current, total) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.summary.current = Number(current) || 0;
  runtime.summary.total = Number(total) || 0;
  renderTaskRuntime(taskEl);
}

function updateQueueWatcher(tasks) {
  const hasQueued = (tasks || []).some((task) => String(task.status || "") === "queued");
  if (hasQueued && !state.queueTimer) {
    state.queueTimer = window.setInterval(() => {
      void refreshQueuePositions();
    }, QUEUE_POLL_INTERVAL_MS);
  } else if (!hasQueued && state.queueTimer) {
    window.clearInterval(state.queueTimer);
    state.queueTimer = null;
  }
}

function updateQueueWatcherFromDom() {
  const hasQueued = Array.from(document.querySelectorAll(".task")).some((taskEl) => {
    return taskEl._runtime && taskEl._runtime.baseStatus === "queued";
  });
  if (hasQueued && !state.queueTimer) {
    state.queueTimer = window.setInterval(() => {
      void refreshQueuePositions();
    }, QUEUE_POLL_INTERVAL_MS);
  } else if (!hasQueued && state.queueTimer) {
    window.clearInterval(state.queueTimer);
    state.queueTimer = null;
  }
}

async function refreshQueuePositions() {
  if (state.queueRefreshInFlight) {
    return;
  }
  state.queueRefreshInFlight = true;
  try {
    const tasks = await api("/api/tasks");
    const byId = new Map(tasks.map((task) => [String(task.id), task]));
    document.querySelectorAll(".task").forEach((taskEl) => {
      const runtime = taskEl._runtime;
      if (!runtime) {
        return;
      }
      const task = byId.get(taskEl.dataset.taskId || "");
      if (!task) {
        return;
      }
      runtime.baseStatus = String(task.status || runtime.baseStatus);
      runtime.queuePosition = parseQueuePosition(task.queue_position);
      if (runtime.baseStatus !== "running") {
        runtime.taskStartedAt = computeTaskStartedAt(task);
      }
      renderTaskRuntime(taskEl);
    });
    updateQueueWatcher(tasks);
  } catch {
    // Ignore transient API errors in queue polling.
  } finally {
    state.queueRefreshInFlight = false;
  }
}

function connectEvents() {
  if (state.eventSource) {
    state.eventSource.close();
  }
  const url = new URL("/api/events", window.location.origin);
  if (state.actingAs) {
    url.searchParams.set("as_user", state.actingAs);
  }
  if (isLocalDevHost()) {
    url.searchParams.set("dev_user", state.authUser);
  }
  state.eventSource = new EventSource(url.toString(), { withCredentials: false });

  state.eventSource.addEventListener("video_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskProgress(payload.task_id, "video", payload.data || {});
  });
  state.eventSource.addEventListener("audio_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskProgress(payload.task_id, "audio", payload.data || {});
  });
  state.eventSource.addEventListener("task_status", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStatus(payload.task_id, payload.data.status);
  });
  state.eventSource.addEventListener("step", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStep(payload.task_id, payload.data.name, payload.data.status);
  });
  state.eventSource.addEventListener("phase", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskPhase(payload.task_id, payload.data.phase, payload.data.status);
  });
  state.eventSource.addEventListener("llama_model_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchLlamaModelProgress(payload.task_id, payload.data.status);
  });
  state.eventSource.addEventListener("transcribe_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchTranscribeProgress(payload.task_id, payload.data.segment_index, payload.data.total);
  });
  state.eventSource.addEventListener("summary_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchSummaryProgress(payload.task_id, payload.data.current, payload.data.total);
  });
  state.eventSource.onerror = () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setTimeout(connectEvents, 2000);
  };
}

async function loadMe() {
  let me;
  try {
    me = await api("/api/me");
  } catch (error) {
    if (state.actingAs) {
      state.actingAs = "";
      localStorage.removeItem("vts_as_user");
      me = await api("/api/me");
    } else {
      throw error;
    }
  }
  state.me = me;
  state.authUser = String(me.requested_by || state.authUser);
  localStorage.setItem("vts_auth_user", state.authUser);
  authUserLabel.textContent = `${me.requested_by}${me.is_admin ? t("context.admin_suffix") : ""}`;
  if (!state.actingAs && me.acting_as !== me.requested_by) {
    state.actingAs = me.acting_as;
    localStorage.setItem("vts_as_user", state.actingAs);
  }
}

async function loadAdminPanel() {
  if (!adminControls || !adminSelect) {
    return;
  }
  if (!state.me || !state.me.is_admin) {
    adminControls.classList.add("hidden");
    return;
  }
  adminControls.classList.remove("hidden");
  const response = await api("/api/admin/users").catch(() => ({ users: [] }));
  const users = new Set(response.users || []);
  users.add(state.me.requested_by);
  if (state.me.acting_as) {
    users.add(state.me.acting_as);
  }
  const sortedUsers = Array.from(users).sort((a, b) => a.localeCompare(b));
  adminSelect.innerHTML = "";
  sortedUsers.forEach((user) => {
    const option = document.createElement("option");
    option.value = user;
    option.textContent = user;
    adminSelect.appendChild(option);
  });
  adminSelect.value = state.me.acting_as;
}

async function applyAdminUser() {
  if (!adminSelect || !state.me) {
    return;
  }
  const selected = adminSelect.value.trim();
  if (!selected) {
    return;
  }
  if (selected === state.me.requested_by) {
    state.actingAs = "";
    localStorage.removeItem("vts_as_user");
  } else {
    state.actingAs = selected;
    localStorage.setItem("vts_as_user", state.actingAs);
  }
  await refreshAll();
}

async function resetAdminUser() {
  state.actingAs = "";
  localStorage.removeItem("vts_as_user");
  await refreshAll();
}

async function refreshAll() {
  await checkServerVersion();
  await loadMe();
  await loadAdminPanel();
  await loadTasks();
  connectEvents();
  startVersionWatcher();
  startDurationTicker();
}

refreshBtn.addEventListener("click", loadTasks);
form.addEventListener("submit", createTask);
form.transcript.addEventListener("change", syncSummaryToggle);
if (adminApplyBtn) {
  adminApplyBtn.addEventListener("click", applyAdminUser);
}
if (adminResetBtn) {
  adminResetBtn.addEventListener("click", resetAdminUser);
}

applyI18nToPage();
setVersionLabel(BUILD_VERSION);
syncSummaryToggle();
refreshAll();

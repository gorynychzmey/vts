const taskList = document.getElementById("task-list");
const taskTemplate = document.getElementById("task-template");
const form = document.getElementById("task-form");
function getSourceType() {
  const checked = document.querySelector('input[name="source-type"]:checked');
  return checked ? checked.value : "url";
}
const authUserLabel = document.getElementById("auth-user");
const adminControls = document.getElementById("admin-controls");
const adminSelect = document.getElementById("admin-user-select");
const adminApplyBtn = document.getElementById("admin-apply-btn");
const adminResetBtn = document.getElementById("admin-reset-btn");
const appVersionLabel = document.getElementById("app-version");
const refreshBtn = document.getElementById("refresh-btn");
const BUILD_VERSION = String(window.__VTS_BUILD_VERSION__ || "0.0.0");
const VERSION_CHECK_INTERVAL_MS = 300000;
const QUEUE_POLL_INTERVAL_MS = 5000;
const LOG_POLL_INTERVAL_MS = 2000;
const ARCHIVED_LOG_MARKER = "__VTS_LOG_ARCHIVED__";

const DAG_STEPS = [
  "download",
  "extract_audio",
  "trim_initial_silence",
  "segment_audio",
  "detect_language",
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
// Relative per-step weights (in seconds) averaged over the last 4 completed pipeline runs.
const STEP_WEIGHT_SECONDS = {
  download: 14.5,
  extract_audio: 6.8,
  trim_initial_silence: 0.5,
  segment_audio: 4.7,
  detect_language: 2.7,
  transcribe_segments: 399.8,
  merge_transcript: 0.1,
  prepare_llama_model: 4.9,
  prepare_summary_chunks: 0.2,
  summarize_windows: 1171.2
};
// Fallback equals average summarize_final duration over the last 4 runs.
const FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS = 369.2;

window.__VTS_I18N = window.__VTS_I18N || {};
const I18N = window.__VTS_I18N || {};
const SUPPORTED_LOCALES = new Set(["en", "ru", "de"]);
const pendingLocaleLoads = new Map();

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
    if (SUPPORTED_LOCALES.has(short)) {
      return short;
    }
  }
  return "en";
}

function localeScriptUrl(locale) {
  const safeLocale = String(locale || "").toLowerCase();
  return `/static/i18n/${safeLocale}.js?v=${encodeURIComponent(BUILD_VERSION)}`;
}

function loadLocaleScript(locale) {
  const safeLocale = String(locale || "").toLowerCase();
  if (!SUPPORTED_LOCALES.has(safeLocale)) {
    return Promise.resolve(false);
  }
  if (I18N[safeLocale]) {
    return Promise.resolve(true);
  }
  const pending = pendingLocaleLoads.get(safeLocale);
  if (pending) {
    return pending;
  }
  const promise = new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = localeScriptUrl(safeLocale);
    script.async = true;
    script.onload = () => resolve(Boolean(I18N[safeLocale]));
    script.onerror = () => resolve(false);
    document.head.appendChild(script);
  });
  pendingLocaleLoads.set(safeLocale, promise);
  return promise.finally(() => {
    pendingLocaleLoads.delete(safeLocale);
  });
}

async function ensureI18nLoaded() {
  const preferred = detectLocale();
  const localeLoaded = await loadLocaleScript(preferred);
  if (preferred !== "en") {
    await loadLocaleScript("en");
  }
  if (localeLoaded) {
    state.locale = preferred;
    return;
  }
  state.locale = "en";
  await loadLocaleScript("en");
}

const state = {
  locale: "en",
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
  const localeDict = I18N[state.locale] || I18N.en || {};
  const fallbackDict = I18N.en || {};
  const raw = localeDict[key] ?? fallbackDict[key] ?? key;
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

function localizeLogText(text) {
  const value = String(text || "");
  if (value.trim() === ARCHIVED_LOG_MARKER) {
    return t("log.archived");
  }
  return value;
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

function stopLogPolling(taskEl) {
  if (!taskEl) {
    return;
  }
  if (taskEl._logPollTimer) {
    window.clearInterval(taskEl._logPollTimer);
    taskEl._logPollTimer = null;
  }
  taskEl._logPollInFlight = false;
  taskEl._forceLogScroll = false;
}

function stopAllLogPolling() {
  document.querySelectorAll(".task").forEach((taskEl) => {
    stopLogPolling(taskEl);
  });
}

async function refreshTaskLog(taskEl, taskId) {
  if (!taskEl || !taskEl.isConnected || taskEl._logPollInFlight || !taskEl._elements) {
    return;
  }
  const panel = taskEl._elements.logPanel;
  if (!panel || !panel.classList.contains("active")) {
    return;
  }
  taskEl._logPollInFlight = true;
  try {
    const text = await api(`/api/tasks/${taskId}/log`);
    if (!taskEl.isConnected) {
      return;
    }
    if (typeof text !== "string") {
      return;
    }
    if (text === taskEl._lastLogRaw) {
      return;
    }
    const renderedText = localizeLogText(text);
    const nearBottom = panel.scrollHeight - (panel.scrollTop + panel.clientHeight) <= 24;
    panel.textContent = renderedText;
    taskEl._lastLogRaw = text;
    taskEl._lastLogText = renderedText;
    if (nearBottom || taskEl._forceLogScroll) {
      panel.scrollTop = panel.scrollHeight;
    }
    taskEl._forceLogScroll = false;
  } catch (error) {
    if (!taskEl.isConnected) {
      return;
    }
    panel.textContent = error.message;
    taskEl._lastLogRaw = "";
    taskEl._lastLogText = "";
  } finally {
    taskEl._logPollInFlight = false;
  }
}

function startLogPolling(taskEl, taskId) {
  stopLogPolling(taskEl);
  taskEl._forceLogScroll = true;
  void refreshTaskLog(taskEl, taskId);
  taskEl._logPollTimer = window.setInterval(() => {
    if (!taskEl.isConnected || !taskEl._elements || !taskEl._elements.logPanel?.classList.contains("active")) {
      stopLogPolling(taskEl);
      return;
    }
    void refreshTaskLog(taskEl, taskId);
  }, LOG_POLL_INTERVAL_MS);
}

function getActiveTabName(taskEl) {
  if (!taskEl) {
    return "";
  }
  const activeBtn = taskEl.querySelector(".tab-btn.active");
  return activeBtn ? String(activeBtn.dataset.tab || "") : "";
}

function getTabPanel(taskEl, tabName) {
  if (!taskEl || !tabName) {
    return null;
  }
  return taskEl.querySelector(`.tab-content.${tabName}`);
}

function getTabButton(taskEl, tabName) {
  if (!taskEl || !tabName) {
    return null;
  }
  return taskEl.querySelector(`.tab-btn[data-tab="${tabName}"]`);
}

function isTabEnabled(taskEl, tabName) {
  const btn = getTabButton(taskEl, tabName);
  return Boolean(btn && !btn.disabled);
}

function getFirstEnabledTab(taskEl) {
  const orderedTabs = ["transcript", "redacted", "summary", "log"];
  for (const tabName of orderedTabs) {
    if (isTabEnabled(taskEl, tabName)) {
      return tabName;
    }
  }
  return "";
}

function ensureActiveTabSelection(taskEl) {
  if (!taskEl) {
    return "";
  }
  const currentTab = getActiveTabName(taskEl);
  if (currentTab && isTabEnabled(taskEl, currentTab)) {
    return currentTab;
  }
  const fallbackTab = getFirstEnabledTab(taskEl);
  if (!fallbackTab) {
    return "";
  }
  taskEl.querySelectorAll(".tab-btn").forEach((item) => item.classList.remove("active"));
  taskEl.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
  getTabButton(taskEl, fallbackTab)?.classList.add("active");
  getTabPanel(taskEl, fallbackTab)?.classList.add("active");
  return fallbackTab;
}

function getTabDownloadSpec(tabName) {
  if (tabName === "transcript") {
    return { prefix: "transcript", ext: "txt" };
  }
  if (tabName === "summary") {
    return { prefix: "summary", ext: "md" };
  }
  if (tabName === "log") {
    return { prefix: "log", ext: "log" };
  }
  return { prefix: "content", ext: "txt" };
}

function buildTabFilename(taskId, tabName) {
  const spec = getTabDownloadSpec(tabName);
  const idPart = String(taskId || "")
    .replace(/[^a-zA-Z0-9_-]/g, "")
    .slice(0, 12);
  const safeId = idPart || "task";
  return `${spec.prefix}-${safeId}.${spec.ext}`;
}

function downloadTextFile(fileName, text) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function copyTextToClipboard(text) {
  if (!text) {
    return false;
  }
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return true;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const ok = typeof document.execCommand === "function" ? document.execCommand("copy") : false;
  textarea.remove();
  return ok;
}

async function loadTabContent(taskEl, taskId, tabName) {
  if (tabName === "log") {
    const panel = getTabPanel(taskEl, "log");
    const text = await api(`/api/tasks/${taskId}/log`).catch((err) => err.message);
    const rawValue = String(text || "");
    const value = localizeLogText(rawValue);
    if (panel) {
      const nearBottom = panel.scrollHeight - (panel.scrollTop + panel.clientHeight) <= 24;
      panel.textContent = value;
      if (nearBottom || taskEl._forceLogScroll) {
        panel.scrollTop = panel.scrollHeight;
      }
      taskEl._forceLogScroll = false;
    }
    taskEl._lastLogRaw = rawValue;
    taskEl._lastLogText = value;
    return value;
  }
  const endpoint = tabName === "transcript" ? "transcript" : tabName === "summary" ? "summary" : tabName === "redacted" ? "redacted" : "";
  if (!endpoint) {
    return "";
  }
  const text = await api(`/api/tasks/${taskId}/${endpoint}`).catch((err) => err.message);
  const value = String(text || "");
  const panel = getTabPanel(taskEl, tabName);
  if (panel) {
    panel.textContent = value;
  }
  return value;
}

async function getActiveTabPayload(taskEl, taskId) {
  const tabName = getActiveTabName(taskEl);
  if (!tabName) {
    return { tabName: "", text: "" };
  }
  let text = String(getTabPanel(taskEl, tabName)?.textContent || "");
  const promptKey = `tab.prompt_${tabName}`;
  const promptValue = t(promptKey);
  if (!text || text === promptValue) {
    text = await loadTabContent(taskEl, taskId, tabName);
  } else if (tabName === "log") {
    text = await loadTabContent(taskEl, taskId, tabName);
  }
  return { tabName, text: String(text || "") };
}

async function copyActiveTabContent(taskEl, taskId) {
  const payload = await getActiveTabPayload(taskEl, taskId);
  if (!payload.text) {
    return;
  }
  try {
    await copyTextToClipboard(payload.text);
  } catch {
    // Ignore clipboard failures (e.g. browser permissions).
  }
}

async function saveActiveTabContent(taskEl, taskId) {
  const payload = await getActiveTabPayload(taskEl, taskId);
  if (!payload.text) {
    return;
  }
  const fileName = buildTabFilename(taskId, payload.tabName);
  downloadTextFile(fileName, payload.text);
}

async function activateTaskTab(taskEl, taskId, tabName) {
  const tab = String(tabName || "");
  if (!tab) {
    return;
  }
  if (!isTabEnabled(taskEl, tab)) {
    return;
  }
  const panel = taskEl.querySelector(`.tab-content.${tab}`);
  if (!panel) {
    return;
  }
  taskEl.querySelectorAll(".tab-btn").forEach((item) => item.classList.remove("active"));
  taskEl.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
  const activeBtn = taskEl.querySelector(`.tab-btn[data-tab="${tab}"]`);
  if (activeBtn) {
    activeBtn.classList.add("active");
  }
  panel.classList.add("active");
  if (tab === "log") {
    startLogPolling(taskEl, taskId);
    return;
  }
  stopLogPolling(taskEl);
  if (tab === "transcript" || tab === "summary" || tab === "redacted") {
    await loadTabContent(taskEl, taskId, tab);
  }
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

function buildStepStatusMap(task) {
  const map = {};
  const steps = Array.isArray(task && task.steps) ? task.steps : [];
  steps.forEach((step) => {
    const name = String(step && step.name ? step.name : "");
    if (!name) {
      return;
    }
    map[name] = String(step && step.status ? step.status : "");
  });
  return map;
}

function isStepFinishedStatus(status) {
  return status === "completed" || status === "skipped";
}

function estimateFinalSummaryWeight(runtime) {
  const summaryTotal = Number(runtime && runtime.summary ? runtime.summary.total : 0);
  const windows = Number.isFinite(summaryTotal) && summaryTotal > 1 ? summaryTotal - 1 : 0;
  if (windows > 0) {
    return STEP_WEIGHT_SECONDS.summarize_windows / windows;
  }
  return FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS;
}

function getStepWeight(runtime, stepName) {
  if (stepName === "summarize_final") {
    return estimateFinalSummaryWeight(runtime);
  }
  const value = STEP_WEIGHT_SECONDS[stepName];
  if (Number.isFinite(value) && value > 0) {
    return value;
  }
  return 1;
}

function getTotalEnabledWeight(runtime) {
  return runtime.enabledSteps.reduce((sum, step) => sum + getStepWeight(runtime, step), 0);
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

function parseFailureCode(value) {
  const code = String(value || "").trim();
  return code || "";
}

function parseErrorMessage(value) {
  const text = String(value || "").trim();
  return text || "";
}

function parseNonNegativeInt(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return null;
  }
  return Math.floor(numeric);
}

function parseTaskStats(task) {
  const stats = task && typeof task === "object" ? task.stats : null;
  return {
    processingSeconds: parseNonNegativeInt(stats && stats.processing_seconds),
    transcriptChars: parseNonNegativeInt(stats && stats.transcript_chars),
    summaryChars: parseNonNegativeInt(stats && stats.summary_chars),
    redactedChars: parseNonNegativeInt(stats && stats.redacted_chars)
  };
}

function detectFailureCode(errorMessage) {
  const text = String(errorMessage || "").toLowerCase();
  if (!text) {
    return "";
  }
  if (
    text.includes("this live event will begin in a few moments") ||
    text.includes("this live event has not started") ||
    text.includes("premieres in")
  ) {
    return "download_live_not_started";
  }
  return "";
}

function resolveFailureMessage(runtime) {
  if (runtime.baseStatus !== "failed") {
    return "";
  }
  const failureCode = runtime.failureCode || detectFailureCode(runtime.failureError);
  let baseMessage = "";
  if (failureCode === "download_live_not_started") {
    baseMessage = t("failure.download_live_not_started");
  } else {
    baseMessage = t("failure.generic");
  }
  if (!runtime.failureError || failureCode === "download_live_not_started") {
    return baseMessage;
  }
  return t("failure.with_error", { message: baseMessage, error: runtime.failureError });
}

function formatMetricNumber(value) {
  return new Intl.NumberFormat(state.locale || "en").format(value);
}

function formatMetricChars(value) {
  if (!Number.isInteger(value) || value < 0) {
    return t("stats.unknown");
  }
  return t("stats.chars", { count: formatMetricNumber(value) });
}

function formatMetricDuration(seconds) {
  if (!Number.isInteger(seconds) || seconds < 0) {
    return t("stats.unknown");
  }
  return formatDuration(seconds);
}

function resolveCompletedMessage(runtime) {
  if (runtime.baseStatus !== "completed") {
    return "";
  }
  return t("success.completed_stats", {
    time: formatMetricDuration(runtime.stats.processingSeconds),
    transcript: formatMetricChars(runtime.stats.transcriptChars),
    redacted: formatMetricChars(runtime.stats.redactedChars),
    summary: formatMetricChars(runtime.stats.summaryChars)
  });
}

function resolveTaskMessage(runtime) {
  const failureMessage = resolveFailureMessage(runtime);
  if (failureMessage) {
    return failureMessage;
  }
  return resolveCompletedMessage(runtime);
}

function readStageProgress(task, stageName) {
  const progress = task && typeof task === "object" ? task.progress : null;
  const stage = progress && typeof progress === "object" ? progress[stageName] : null;
  const current = Number(stage && stage.current);
  const total = Number(stage && stage.total);
  return {
    current: Number.isFinite(current) && current > 0 ? current : 0,
    total: Number.isFinite(total) && total > 0 ? total : 0
  };
}

function createRuntime(task) {
  const runningStep = findStep(task, "running");
  const failedStep = findStep(task, "failed");
  const enabledSteps = getEnabledSteps(task);
  const stepStatusByName = buildStepStatusMap(task);
  const transcribeProgress = readStageProgress(task, "transcribe");
  const summaryProgress = readStageProgress(task, "summary");
  return {
    sourceUrl: String(task.source_url || ""),
    displayName: typeof task.source_title === "string" ? task.source_title.trim() : "",
    baseStatus: String(task.status || ""),
    failureCode: parseFailureCode(task.failure_code),
    failureError: parseErrorMessage(task.error_message),
    queuePosition: parseQueuePosition(task.queue_position),
    enabledSteps,
    stepStatusByName,
    transcriptReady: Boolean(task.transcript_path),
    summaryExpected: enabledSteps.includes("summarize_final"),
    summaryReady: Boolean(task.summary_path),
    redactedReady: Boolean(task.redacted_path),
    mediaReady: Boolean(task.media_path),
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
      current: transcribeProgress.current,
      total: transcribeProgress.total
    },
    segment: {
      current: 0,
      total: 0
    },
    summary: {
      current: summaryProgress.current,
      total: summaryProgress.total
    },
    stats: parseTaskStats(task)
  };
}

function resolveActiveStep(runtime) {
  if (runtime.currentStepName && runtime.enabledSteps.includes(runtime.currentStepName)) {
    return runtime.currentStepName;
  }
  const runningFromSnapshot = runtime.enabledSteps.find((step) => runtime.stepStatusByName[step] === "running");
  if (runningFromSnapshot) {
    return runningFromSnapshot;
  }
  if (runtime.mediaPhase || runtime.download.hasVideo || runtime.download.hasAudio) {
    return "download";
  }
  if (runtime.failedStepName) {
    return runtime.failedStepName;
  }
  const failedFromSnapshot = runtime.enabledSteps.find((step) => runtime.stepStatusByName[step] === "failed");
  if (failedFromSnapshot) {
    return failedFromSnapshot;
  }
  if (runtime.baseStatus === "running") {
    const firstIncomplete = runtime.enabledSteps.find(
      (step) => !isStepFinishedStatus(runtime.stepStatusByName[step] || "")
    );
    if (firstIncomplete) {
      return firstIncomplete;
    }
  }
  if (runtime.baseStatus === "queued" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[0];
  }
  if (runtime.baseStatus === "completed" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[runtime.enabledSteps.length - 1];
  }
  return "";
}

function computeActiveStepLocalProgress(runtime, active) {
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
    }
    // else: value = 0, indeterminate = false → показываем 0% пока не получен total
  } else if (active === "segment_audio") {
    if (runtime.segment.total > 0) {
      const current = Math.max(0, Math.min(runtime.segment.current, runtime.segment.total));
      value = normalizeProgress(current / runtime.segment.total);
      textOverride = `${current}/${runtime.segment.total}`;
    }
    // else: value = 0, indeterminate = false → показываем 0% пока не получен total
  } else if (active === "summarize_windows") {
    if (runtime.summary.total > 1) {
      const totalWindows = runtime.summary.total - 1;
      const currentWindows = Math.max(0, Math.min(runtime.summary.current, totalWindows));
      value = normalizeProgress(currentWindows / totalWindows);
      textOverride = `${currentWindows}/${totalWindows}`;
    }
    // else: value = 0, indeterminate = false → показываем 0% пока не получен total
  } else if (active === "summarize_final") {
    const finalStatus = runtime.stepStatusByName.summarize_final || "";
    if (finalStatus === "completed") {
      value = 1;
    } else {
      value = 0;
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

  return { value, indeterminate, textOverride };
}

function computeLocalStepProgress(runtime) {
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
  if (!active) {
    return { value: 0.05, indeterminate: true, text: t("progress.working") };
  }
  const local = computeActiveStepLocalProgress(runtime, active);
  const normalizedValue = normalizeProgress(local.value);
  const displayValue = local.indeterminate ? Math.max(0.05, normalizedValue) : normalizedValue;
  if (local.textOverride) {
    return { value: displayValue, indeterminate: local.indeterminate, text: local.textOverride };
  }
  if (local.indeterminate) {
    return { value: displayValue, indeterminate: true, text: t("progress.working") };
  }
  return { value: displayValue, indeterminate: false, text: `${Math.round(displayValue * 100)}%` };
}

function computeOverallProgress(runtime) {
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
  const local = computeActiveStepLocalProgress(runtime, active);
  const totalWeight = getTotalEnabledWeight(runtime);
  if (!(totalWeight > 0)) {
    return { value: 0.05, indeterminate: true, text: t("progress.working") };
  }

  let doneWeight = 0;
  runtime.enabledSteps.forEach((stepName) => {
    const status = runtime.stepStatusByName[stepName] || "";
    if (isStepFinishedStatus(status)) {
      doneWeight += getStepWeight(runtime, stepName);
    }
  });

  const activeStatus = active ? runtime.stepStatusByName[active] || "" : "";
  if (active && runtime.enabledSteps.includes(active) && !isStepFinishedStatus(activeStatus)) {
    const activeWeight = getStepWeight(runtime, active);
    const localValue = local.indeterminate ? Math.max(0.05, local.value) : local.value;
    doneWeight += activeWeight * normalizeProgress(localValue);
  }

  const overall = normalizeProgress(doneWeight / totalWeight);
  return { value: overall, indeterminate: false, text: `${Math.round(overall * 100)}%` };
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
  const canResume = runtime.baseStatus === "paused" || runtime.baseStatus === "failed";
  const failedSummaryStep = runtime.enabledSteps.find(
    (stepName) => SUMMARY_STEPS.has(stepName) && runtime.stepStatusByName[stepName] === "failed"
  );
  const canRestartSummary =
    runtime.summaryExpected &&
    (runtime.baseStatus === "completed" || (runtime.baseStatus === "failed" && Boolean(failedSummaryStep)));
  const windowsCompleted = runtime.stepStatusByName["summarize_windows"] === "completed";
  const finalFailed = runtime.stepStatusByName["summarize_final"] === "failed";
  const canRestartFinalSummary =
    runtime.summaryExpected &&
    windowsCompleted &&
    (runtime.baseStatus === "completed" || (runtime.baseStatus === "failed" && finalFailed));
  const canArchive = runtime.baseStatus === "completed" || runtime.baseStatus === "failed";
  elements.pauseBtn.disabled = !canPause;
  elements.resumeBtn.disabled = !canResume;
  if (elements.restartSummaryBtn) {
    elements.restartSummaryBtn.disabled = !canRestartSummary;
  }
  if (elements.restartSummaryFinalBtn) {
    elements.restartSummaryFinalBtn.disabled = !canRestartFinalSummary;
  }
  if (elements.downloadMediaBtn) {
    elements.downloadMediaBtn.disabled = !runtime.mediaReady;
  }
  if (elements.archiveBtn) {
    elements.archiveBtn.disabled = !canArchive;
  }
  const canOpenTranscript = runtime.transcriptReady;
  elements.transcriptTabBtn.disabled = !canOpenTranscript;
  elements.transcriptTabBtn.title = canOpenTranscript ? t("tab.transcript") : t("tab.transcript_pending");
  elements.transcriptTabBtn.setAttribute("aria-label", elements.transcriptTabBtn.title);
  const canOpenSummary = runtime.summaryReady;
  elements.summaryTabBtn.disabled = !canOpenSummary;
  elements.summaryTabBtn.title = canOpenSummary ? t("tab.summary") : t("tab.summary_pending");
  elements.summaryTabBtn.setAttribute("aria-label", elements.summaryTabBtn.title);
  if (elements.redactedTabBtn) {
    const canOpenRedacted = runtime.redactedReady;
    elements.redactedTabBtn.disabled = !canOpenRedacted;
    elements.redactedTabBtn.title = canOpenRedacted ? t("tab.redacted") : t("tab.redacted_pending");
    elements.redactedTabBtn.setAttribute("aria-label", elements.redactedTabBtn.title);
  }
  ensureActiveTabSelection(taskEl);

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

  const overallProgress = computeOverallProgress(runtime);
  elements.overallProgressWrap.classList.toggle("indeterminate", overallProgress.indeterminate);
  elements.overallProgressFill.style.width = `${Math.round(overallProgress.value * 100)}%`;
  elements.overallProgressText.textContent = overallProgress.text;
  elements.overallProgressWrap.setAttribute("aria-valuenow", String(Math.round(overallProgress.value * 100)));

  const localProgress = computeLocalStepProgress(runtime);
  elements.localProgressWrap.classList.toggle("indeterminate", localProgress.indeterminate);
  elements.localProgressFill.style.width = `${Math.round(localProgress.value * 100)}%`;
  elements.localProgressText.textContent = localProgress.text;
  elements.localProgressWrap.setAttribute("aria-valuenow", String(Math.round(localProgress.value * 100)));

  const taskMessage = resolveTaskMessage(runtime);
  if (elements.messageEl) {
    elements.messageEl.textContent = taskMessage;
    elements.messageEl.classList.toggle("hidden", !taskMessage);
  }
}

function renderTasks(tasks) {
  stopAllLogPolling();
  taskList.innerHTML = "";
  tasks.forEach((task) => {
    const node = taskTemplate.content.cloneNode(true);
    const root = node.querySelector(".task");
    const body = node.querySelector(".task-body");
    const toggleBtn = root.querySelector(".toggle-btn");
    const taskRightTop = root.querySelector(".task-right-top");
    const toolbarWrap = root.querySelector(".task-toolbar-wrap");
    const toolbarScroll = root.querySelector(".task-right-bottom");
    const pauseBtn = root.querySelector(".pause-btn");
    const resumeBtn = root.querySelector(".resume-btn");
    const restartSummaryBtn = root.querySelector(".restart-summary-btn");
    const restartSummaryMenu = root.querySelector(".restart-summary-menu");
    const restartSummaryFullBtn = root.querySelector(".restart-summary-full-btn");
    const restartSummaryFinalBtn = root.querySelector(".restart-summary-final-btn");
    const downloadMediaBtn = root.querySelector(".download-media-btn");
    const archiveBtn = root.querySelector(".archive-btn");
    const deleteBtn = root.querySelector(".delete-btn");
    const transcriptPre = root.querySelector(".tab-content.transcript");
    const summaryPre = root.querySelector(".tab-content.summary");
    const redactedPre = root.querySelector(".tab-content.redacted");
    const logPre = root.querySelector(".tab-content.log");
    const transcriptTabBtn = root.querySelector('.tab-btn[data-tab="transcript"]');
    const summaryTabBtn = root.querySelector('.tab-btn[data-tab="summary"]');
    const redactedTabBtn = root.querySelector('.tab-btn[data-tab="redacted"]');
    const copyTabBtn = root.querySelector(".tab-copy-btn");
    const saveTabBtn = root.querySelector(".tab-save-btn");

    applyI18n(root);

    root.dataset.taskId = task.id;
    transcriptPre.textContent = t("tab.prompt_transcript");
    summaryPre.textContent = t("tab.prompt_summary");
    if (redactedPre) {
      redactedPre.textContent = t("tab.prompt_redacted");
    }
    logPre.textContent = t("tab.prompt_log");

    pauseBtn.title = t("action.pause");
    pauseBtn.setAttribute("aria-label", t("action.pause"));
    resumeBtn.title = t("action.resume");
    resumeBtn.setAttribute("aria-label", t("action.resume"));
    if (restartSummaryBtn) {
      restartSummaryBtn.title = t("action.restart_summary");
      restartSummaryBtn.setAttribute("aria-label", t("action.restart_summary"));
    }
    if (restartSummaryFullBtn) {
      restartSummaryFullBtn.textContent = t("action.restart_summary_full");
    }
    if (restartSummaryFinalBtn) {
      restartSummaryFinalBtn.textContent = t("action.restart_summary_final");
    }
    if (downloadMediaBtn) {
      downloadMediaBtn.title = t("action.download_media");
      downloadMediaBtn.setAttribute("aria-label", t("action.download_media"));
    }
    if (archiveBtn) {
      archiveBtn.title = t("action.archive");
      archiveBtn.setAttribute("aria-label", t("action.archive"));
    }
    deleteBtn.title = t("action.delete");
    deleteBtn.setAttribute("aria-label", t("action.delete"));
    toggleBtn.title = t("action.expand");
    toggleBtn.setAttribute("aria-label", t("action.expand"));

    root.querySelectorAll(".tab-btn").forEach((btn) => {
      const tabName = String(btn.dataset.tab || "");
      const tabLabel = t(`tab.${tabName}`);
      btn.textContent = tabLabel === `tab.${tabName}` ? tabName : tabLabel;
    });

    const doToggle = () => {
      body.classList.toggle("hidden");
      const expanded = !body.classList.contains("hidden");
      toggleBtn.classList.toggle("expanded", expanded);
      const label = expanded ? t("action.collapse") : t("action.expand");
      toggleBtn.title = label;
      toggleBtn.setAttribute("aria-label", label);
      if (expanded) {
        const activeTab = ensureActiveTabSelection(root);
        if (activeTab) {
          void activateTaskTab(root, task.id, activeTab);
        }
      } else {
        stopLogPolling(root);
      }
    };
    taskRightTop.addEventListener("click", doToggle);
    toggleBtn.addEventListener("click", (e) => { e.stopPropagation(); doToggle(); });
    if (toolbarWrap && toolbarScroll) {
      const updateFade = () => {
        const atEnd = toolbarScroll.scrollLeft + toolbarScroll.clientWidth >= toolbarScroll.scrollWidth - 1;
        toolbarWrap.classList.toggle("scrolled-end", atEnd);
      };
      toolbarScroll.addEventListener("scroll", updateFade, { passive: true });
      updateFade();
    }
    pauseBtn.addEventListener("click", () => pauseTask(task.id));
    resumeBtn.addEventListener("click", () => resumeTask(task.id));
    if (restartSummaryBtn && restartSummaryMenu) {
      restartSummaryBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = restartSummaryMenu.classList.contains("open");
        document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
        if (!isOpen) {
          const rect = restartSummaryBtn.getBoundingClientRect();
          restartSummaryMenu.style.top = `${rect.bottom + 4}px`;
          restartSummaryMenu.style.left = "0px";
          restartSummaryMenu.classList.add("open");
          restartSummaryMenu.style.left = `${rect.right - restartSummaryMenu.offsetWidth}px`;
        }
      });
    }
    if (restartSummaryFullBtn) {
      restartSummaryFullBtn.addEventListener("click", () => {
        restartSummaryMenu && restartSummaryMenu.classList.remove("open");
        restartSummary(task.id, "full");
      });
    }
    if (restartSummaryFinalBtn) {
      restartSummaryFinalBtn.addEventListener("click", () => {
        restartSummaryMenu && restartSummaryMenu.classList.remove("open");
        restartSummary(task.id, "final_only");
      });
    }
    if (downloadMediaBtn) {
      downloadMediaBtn.addEventListener("click", () => downloadMedia(task.id, task.source_title, downloadMediaBtn));
    }
    if (archiveBtn) {
      archiveBtn.addEventListener("click", () => archiveTask(task.id));
    }
    deleteBtn.addEventListener("click", () => removeTask(task.id));
    if (copyTabBtn) {
      copyTabBtn.addEventListener("click", async () => {
        await copyActiveTabContent(root, task.id);
      });
    }
    if (saveTabBtn) {
      saveTabBtn.addEventListener("click", async () => {
        await saveActiveTabContent(root, task.id);
      });
    }

    root.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (btn.disabled) {
          return;
        }
        await activateTaskTab(root, task.id, String(btn.dataset.tab || ""));
      });
    });

    root._elements = {
      linkEl: root.querySelector(".task-link"),
      sourceEl: root.querySelector(".task-source"),
      statusEl: root.querySelector(".task-status"),
      taskRuntimeEl: root.querySelector(".task-runtime"),
      pauseBtn,
      resumeBtn,
      restartSummaryBtn,
      restartSummaryMenu,
      restartSummaryFinalBtn,
      downloadMediaBtn,
      archiveBtn,
      transcriptTabBtn,
      summaryTabBtn,
      redactedTabBtn,
      copyTabBtn,
      saveTabBtn,
      transcriptPanel: transcriptPre,
      summaryPanel: summaryPre,
      redactedPanel: redactedPre,
      logPanel: logPre,
      stepLabelEl: root.querySelector(".step-label"),
      stepTimeEl: root.querySelector(".step-time"),
      overallProgressWrap: root.querySelector(".overall-progress"),
      overallProgressFill: root.querySelector(".overall-progress .step-progress-fill"),
      overallProgressText: root.querySelector(".overall-progress .step-progress-text"),
      localProgressWrap: root.querySelector(".local-progress"),
      localProgressFill: root.querySelector(".local-progress .step-progress-fill"),
      localProgressText: root.querySelector(".local-progress .step-progress-text"),
      messageEl: root.querySelector(".task-message")
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

function syncSourceType() {
  const isFile = getSourceType() === "file";
  const urlInput = form.url;
  const fileInput = document.getElementById("file-input");
  if (!fileInput) return;
  if (isFile) {
    urlInput.classList.add("hidden");
    urlInput.required = false;
    fileInput.classList.remove("hidden");
    fileInput.required = true;
  } else {
    urlInput.classList.remove("hidden");
    urlInput.required = true;
    fileInput.classList.add("hidden");
    fileInput.required = false;
  }
}

function uploadFileWithProgress(fd) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  if (fill) fill.style.strokeDashoffset = circumference;

  function setProgress(ratio) {
    if (fill) fill.style.strokeDashoffset = circumference * (1 - ratio);
  }

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildPath("/api/tasks/upload"));
    xhr.setRequestHeader("X-Forwarded-User", state.authUser);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      setProgress(1);
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        let msg = `HTTP ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText)?.detail || msg; } catch (_) {}
        reject(new Error(msg));
      }
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.send(fd);
  }).finally(() => {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
    if (fill) fill.style.strokeDashoffset = circumference;
  });
}

async function createTask(event) {
  event.preventDefault();
  const isFile = getSourceType() === "file";
  const fileInput = document.getElementById("file-input");
  if (isFile && fileInput) {
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    if (form.language.value) fd.append("language", form.language.value);
    fd.append("audio_only", form.audio_only.checked ? "true" : "false");
    fd.append("transcript", form.transcript.checked ? "true" : "false");
    fd.append("summary", form.summary.checked ? "true" : "false");
    await uploadFileWithProgress(fd);
  } else {
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
  }
  form.reset();
  form.transcript.checked = true;
  form.summary.checked = true;
  syncSummaryToggle();
  syncSourceType();
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

function apiBatchPost(url, body, method = "POST") {
  return api(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function pauseTask(taskId) {
  await apiBatchPost("/api/tasks/pause", { task_ids: [taskId] });
  await loadTasks();
}

async function resumeTask(taskId) {
  await apiBatchPost("/api/tasks/resume", { task_ids: [taskId] });
  await loadTasks();
}

async function removeTask(taskId) {
  const confirmed = window.confirm(t("confirm.delete"));
  if (!confirmed) {
    return;
  }
  await apiBatchPost("/api/tasks", { task_ids: [taskId] }, "DELETE");
  await loadTasks();
}

function buildMediaFilename(taskId, sourceTitle, serverFilename) {
  const ext = serverFilename ? serverFilename.replace(/^.*(\.[^.]+)$/, "$1") : "";
  const base = sourceTitle && sourceTitle.trim()
    ? sourceTitle.trim().replace(/[\\/:*?"<>|]/g, "_").replace(/\s+/g, " ").slice(0, 200)
    : String(taskId || "media").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 36) || "media";
  return base + ext;
}

async function downloadMedia(taskId, sourceTitle, btn) {
  if (btn) btn.classList.add("loading");
  try {
    const headers = { "X-Forwarded-User": state.authUser };
    const resp = await fetch(buildPath(`/api/tasks/${encodeURIComponent(taskId)}/media`), { headers });
    if (!resp.ok) {
      return;
    }
    const disposition = resp.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename\*?=['"]?(?:UTF-8'')?([^'";]+)['"]?/i);
    const serverFilename = match ? decodeURIComponent(match[1]) : "";
    const filename = buildMediaFilename(taskId, sourceTitle, serverFilename);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } finally {
    if (btn) btn.classList.remove("loading");
  }
}

async function archiveTask(taskId) {
  const confirmed = window.confirm(t("confirm.archive"));
  if (!confirmed) {
    return;
  }
  await apiBatchPost("/api/tasks/archive", { task_ids: [taskId] });
  await loadTasks();
}

async function restartSummary(taskId, mode = "full") {
  const confirmKey = mode === "final_only" ? "confirm.restart_summary_final" : "confirm.restart_summary";
  const confirmed = window.confirm(t(confirmKey));
  if (!confirmed) {
    return;
  }
  await apiBatchPost("/api/tasks/restart_summary", { task_ids: [taskId], mode });
  await loadTasks();
}

function findTaskEl(taskId) {
  return document.querySelector(`[data-task-id="${taskId}"]`);
}

function patchTaskStatus(taskId, status, errorMessage = "", failureCode = "") {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.baseStatus = String(status || "");
  if (runtime.baseStatus === "failed") {
    runtime.failureError = parseErrorMessage(errorMessage);
    runtime.failureCode = parseFailureCode(failureCode) || detectFailureCode(runtime.failureError);
  } else {
    runtime.failureError = "";
    runtime.failureCode = "";
  }
  if (runtime.baseStatus !== "queued") {
    runtime.queuePosition = null;
  }
  if (runtime.baseStatus === "running" && !runtime.taskStartedAt) {
    runtime.taskStartedAt = Date.now();
  }
  if (runtime.baseStatus === "completed" && runtime.summaryExpected) {
    runtime.summaryReady = true;
    void refreshQueuePositions();
  }
  if (runtime.baseStatus === "completed" || runtime.baseStatus === "failed") {
    void api(`/api/tasks/${taskId}`).then((task) => {
      if (taskEl._runtime === runtime && task) {
        if (task.stats) runtime.stats = parseTaskStats(task);
        runtime.mediaReady = Boolean(task.media_path);
        renderTaskRuntime(taskEl);
      }
    }).catch(() => {});
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
  if (stepName) {
    runtime.stepStatusByName[stepName] = stepStatus;
  }
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
  } else if (stepStatus === "completed" || stepStatus === "skipped") {
    if (runtime.currentStepName === stepName) {
      runtime.currentStepName = "";
      runtime.currentStepStartedAt = null;
    }
  }
  if (stepStatus === "completed" && stepName === "merge_transcript") {
    runtime.transcriptReady = true;
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
  if (!runtime.currentStepName && runtime.baseStatus === "running") {
    runtime.currentStepName = "download";
  }
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

function patchSegmentProgress(taskId, current, total) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.segment.current = Number(current) || 0;
  runtime.segment.total = Number(total) || 0;
  if (runtime.baseStatus === "running" && !runtime.currentStepName) {
    runtime.currentStepName = "segment_audio";
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
  if (phaseStatus === "running" && (phaseName === "video" || phaseName === "audio") && !runtime.currentStepName) {
    runtime.currentStepName = "download";
  }
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

function appendStreamingText(taskId, readyFlag, panelKey, promptKey, text, separator) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  if (!runtime[readyFlag]) {
    runtime[readyFlag] = true;
    renderTaskRuntime(taskEl);
  }
  const panel = taskEl._elements && taskEl._elements[panelKey];
  if (!panel) {
    return;
  }
  if (panel.textContent === t(promptKey)) {
    panel.textContent = "";
  }
  const nearBottom = panel.scrollHeight - (panel.scrollTop + panel.clientHeight) <= 24;
  panel.textContent += String(text || "") + separator;
  if (nearBottom) {
    panel.scrollTop = panel.scrollHeight;
  }
}

function appendTranscriptSegment(taskId, text) {
  appendStreamingText(taskId, "transcriptReady", "transcriptPanel", "tab.prompt_transcript", text, " ");
}

function appendRedactedSegment(taskId, text) {
  appendStreamingText(taskId, "redactedReady", "redactedPanel", "tab.prompt_redacted", text, "\n");
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
    const positions = await api("/api/tasks/queue-positions");
    document.querySelectorAll(".task").forEach((taskEl) => {
      const runtime = taskEl._runtime;
      if (!runtime) {
        return;
      }
      const taskId = taskEl.dataset.taskId || "";
      const pos = positions[taskId];
      runtime.queuePosition = parseQueuePosition(pos !== undefined ? pos : null);
      renderTaskRuntime(taskEl);
    });
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

  state.eventSource.addEventListener("server_version", (event) => {
    const payload = JSON.parse(event.data);
    const serverVersion = String(payload.version || "");
    setVersionLabel(serverVersion || BUILD_VERSION);
    if (serverVersion && serverVersion !== BUILD_VERSION) {
      forceReloadToVersion(serverVersion);
    }
  });

  state.eventSource.addEventListener("media_progress", (event) => {
    const payload = JSON.parse(event.data);
    const phase = String((payload.data && payload.data.phase) || "");
    patchTaskProgress(payload.task_id, phase, payload.data || {});
  });
  state.eventSource.addEventListener("task_status", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStatus(payload.task_id, payload.data.status, payload.data.error, payload.data.failure_code);
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
  state.eventSource.addEventListener("segment_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchSegmentProgress(payload.task_id, payload.data.current, payload.data.total);
  });
  state.eventSource.addEventListener("summary_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchSummaryProgress(payload.task_id, payload.data.current, payload.data.total);
  });
  state.eventSource.addEventListener("transcript_segment_text", (event) => {
    const payload = JSON.parse(event.data);
    appendTranscriptSegment(payload.task_id, payload.data.text);
  });
  state.eventSource.addEventListener("segment_summary_text", (event) => {
    const payload = JSON.parse(event.data);
    appendRedactedSegment(payload.task_id, payload.data.text);
  });
  state.eventSource.onerror = () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setTimeout(() => {
      connectEvents();
      void loadTasks();
    }, 2000);
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

document.addEventListener("click", () => {
  document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
});

refreshBtn.addEventListener("click", loadTasks);
form.addEventListener("submit", createTask);
form.transcript.addEventListener("change", syncSummaryToggle);
document.querySelectorAll('input[name="source-type"]').forEach((el) => {
  el.addEventListener("change", syncSourceType);
});
if (adminApplyBtn) {
  adminApplyBtn.addEventListener("click", applyAdminUser);
}
if (adminResetBtn) {
  adminResetBtn.addEventListener("click", resetAdminUser);
}

async function bootstrap() {
  await ensureI18nLoaded();
  applyI18nToPage();
  setVersionLabel(BUILD_VERSION);
  syncSummaryToggle();
  syncSourceType();
  await refreshAll();
}

void bootstrap();

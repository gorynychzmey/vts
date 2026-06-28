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
const promptSelect = document.getElementById("prompt-select");
const BUILD_VERSION = String(window.__VTS_BUILD_VERSION__ || "0.0.0");
const VERSION_CHECK_INTERVAL_MS = 300000;
const QUEUE_POLL_INTERVAL_MS = 5000;
const LOG_POLL_INTERVAL_MS = 2000;
const ARCHIVED_LOG_MARKER = "__VTS_LOG_ARCHIVED__";

// Mirrors server-side vts/pipeline/types.py DAG_HEAD (the static, non-finalize
// part of the pipeline). The finalize tail is built dynamically per selected
// prompt in getEnabledSteps (one finalize step per options.prompts entry).
const DAG_HEAD = [
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
  "pack_window_notes"
];
// Transcript-only head: the steps that run regardless of whether any prompt is
// selected. The summary-head steps below only run when >=1 prompt is selected.
const TRANSCRIPT_HEAD = [
  "download",
  "extract_audio",
  "trim_initial_silence",
  "segment_audio",
  "detect_language",
  "transcribe_segments",
  "merge_transcript"
];
// Back-compat alias kept for any legacy references (full static summary path).
const DAG_STEPS = [...DAG_HEAD, "summarize_final"];
const SUMMARY_STEPS = new Set([
  "prepare_llama_model",
  "prepare_summary_chunks",
  "summarize_windows",
  "pack_window_notes",
  "summarize_final"
]);
// Relative per-step weights (in seconds) averaged over the last 4 completed
// pipeline runs.
// TODO(vts-b6t): recalibrate these constants (and FINAL_SUMMARY_WEIGHT_FALLBACK
// _SECONDS below) against accumulated run metrics once fresh per-step durations
// are available; current values are a stale 4-run average, not yet re-measured
// for the per-prompt finalize fan-out.
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
  const response = await fetch(buildPath(path), { ...options, headers });
  if (response.status === 401 && path.startsWith("/api/")) {
    const here = window.location.pathname + window.location.search;
    window.location.href = "/auth/login?next=" + encodeURIComponent(here);
    return new Promise(() => {});  // never resolves; navigation pending
  }
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
  if (tabName === "summary") {
    // The summary tab is the "results" view: route through the selected prompt
    // result rather than the fixed /summary endpoint.
    return loadSelectedResult(taskEl, taskId);
  }
  const endpoint = tabName === "transcript" ? "transcript" : tabName === "redacted" ? "redacted" : "";
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

function resultEntryLabel(entry) {
  const source = String(entry && entry.source ? entry.source : "");
  const name = String(entry && entry.name ? entry.name : "");
  if (source === "system" && name) {
    const translated = t(name);
    return translated === name ? name : translated;
  }
  return name || `${source}:${entry && entry.id ? entry.id : ""}`;
}

function resultEntryValue(entry) {
  return `${entry && entry.source ? entry.source : ""}:${entry && entry.id ? entry.id : ""}`;
}

// Populate the per-task result dropdown from runtime.promptResults. Preserves
// the current selection if it is still present, otherwise defaults to
// system:summary if available, else the first completed entry.
function renderResultPromptSelect(taskEl) {
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  const select = taskEl._elements.resultPromptSelect;
  if (!select) {
    return;
  }
  const entries = Array.isArray(taskEl._runtime.promptResults) ? taskEl._runtime.promptResults : [];
  const previous = select.value;
  select.innerHTML = "";
  for (const entry of entries) {
    const value = resultEntryValue(entry);
    const completed = String(entry && entry.status ? entry.status : "") === "completed";
    const option = document.createElement("option");
    option.value = value;
    option.disabled = !completed;
    option.textContent = completed ? resultEntryLabel(entry) : `${resultEntryLabel(entry)}${t("results.pending")}`;
    select.appendChild(option);
  }
  const hasValue = (val) =>
    val && entries.some((e) => resultEntryValue(e) === val && String(e.status || "") === "completed");
  let target = "";
  if (hasValue(previous)) {
    target = previous;
  } else if (hasValue("system:summary")) {
    target = "system:summary";
  } else {
    const firstCompleted = entries.find((e) => String(e.status || "") === "completed");
    target = firstCompleted ? resultEntryValue(firstCompleted) : "";
  }
  if (target) {
    select.value = target;
  }
  // Show the dropdown only when there is more than one result to choose from;
  // a single (summary-only) result needs no picker.
  const completedCount = entries.filter((e) => String(e.status || "") === "completed").length;
  if (taskEl._elements.resultPromptBar) {
    taskEl._elements.resultPromptBar.classList.toggle("hidden", entries.length <= 1 && completedCount <= 1);
  }
}

// Load the text for the currently selected result into the summary/results
// panel. Falls back to the legacy /summary endpoint when no result is selected
// (e.g. summary-only task before prompt_results is populated).
async function loadSelectedResult(taskEl, taskId) {
  const select = taskEl && taskEl._elements ? taskEl._elements.resultPromptSelect : null;
  const panel = getTabPanel(taskEl, "summary");
  const value = select ? String(select.value || "") : "";
  let text;
  if (value) {
    const idx = value.indexOf(":");
    const source = idx >= 0 ? value.slice(0, idx) : value;
    const ref = idx >= 0 ? value.slice(idx + 1) : "";
    text = await api(
      `/api/tasks/${taskId}/results/${encodeURIComponent(source)}/${encodeURIComponent(ref)}`
    ).catch((err) => err.message);
  } else {
    text = await api(`/api/tasks/${taskId}/summary`).catch((err) => err.message);
  }
  const out = String(text || "");
  if (panel) {
    panel.textContent = out;
  }
  return out;
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
  // The result-prompt picker belongs to the summary/results tab only.
  if (taskEl._elements && taskEl._elements.resultPromptBar) {
    if (tab === "summary") {
      renderResultPromptSelect(taskEl);
    } else {
      taskEl._elements.resultPromptBar.classList.add("hidden");
    }
  }
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

// Mirrors vts/pipeline/types.py finalize_step_name.
function finalizeStepName(source, id) {
  if (source === "system" && id === "summary") {
    return "summarize_final";
  }
  return `finalize:${source}:${id}`;
}

// Mirrors vts/services/task_progress.py selected_prompt_refs: returns the list
// of {source, id} the pipeline will finalize. Prefers the explicit
// options.prompts list; falls back to legacy options.summary semantics.
function selectedPromptRefs(options) {
  if (Array.isArray(options.prompts)) {
    const refs = [];
    for (const entry of options.prompts) {
      let source = "";
      let id = "";
      if (typeof entry === "string") {
        const idx = entry.indexOf(":");
        source = idx >= 0 ? entry.slice(0, idx) : entry;
        id = idx >= 0 ? entry.slice(idx + 1) : "";
      } else if (entry && typeof entry === "object") {
        source = String(entry.source || "");
        id = String(entry.id || "");
      }
      if ((source === "system" || source === "user") && id) {
        refs.push({ source, id });
      }
    }
    return refs;
  }
  // Legacy fallback: no prompts list -> one summary unless summary disabled.
  if (options.summary === false) {
    return [];
  }
  return [{ source: "system", id: "summary" }];
}

// Mirrors server build_dag_steps: head + one finalize step per selected prompt.
// The summary-head steps (prepare_llama_model..pack_window_notes) only run when
// at least one prompt is selected (server gates them on selected_prompt_refs).
function getEnabledSteps(task) {
  const options = task.options || {};
  const transcriptEnabled = options.transcript !== false;
  if (!transcriptEnabled) {
    return ["download"];
  }
  const refs = selectedPromptRefs(options);
  if (refs.length === 0) {
    // No prompts selected: no summarization work, so omit the summary-head and
    // any finalize steps.
    return [...TRANSCRIPT_HEAD];
  }
  const tail = refs.map((ref) => finalizeStepName(ref.source, ref.id));
  return [...DAG_HEAD, ...tail];
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
  // Every finalize step (the summary's "summarize_final" plus each custom
  // prompt's "finalize:source:id") is roughly one final-summary call.
  if (stepName === "summarize_final" || stepName.startsWith("finalize:")) {
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
    redactedChars: parseNonNegativeInt(stats && stats.redacted_chars),
    mediaSeconds: parseNonNegativeInt(stats && stats.media_seconds),
    mediaBytes: parseNonNegativeInt(stats && stats.media_bytes)
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

function formatMegabytes(bytes) {
  // One decimal place, locale-aware. 1 MB = 1024*1024 bytes (binary MB,
  // matching what file managers report). Any nonzero size floors to 0.1 so a
  // tiny-but-present file never reads as "0.0 MB".
  const mb = bytes / (1024 * 1024);
  const rounded = mb > 0 ? Math.max(0.1, mb) : 0;
  return new Intl.NumberFormat(state.locale || "en", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1
  }).format(rounded);
}

// Compact "duration · size MB" line under the task link, filled in as the
// media file becomes available. Hidden until at least one metric is known.
function renderTaskStats(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  if (!runtime || !elements || !elements.statsEl) {
    return;
  }
  const stats = runtime.stats || {};
  const parts = [];
  if (Number.isInteger(stats.mediaSeconds) && stats.mediaSeconds > 0) {
    parts.push(t("stats.media_duration", { duration: formatDuration(stats.mediaSeconds) }));
  }
  if (Number.isInteger(stats.mediaBytes) && stats.mediaBytes > 0) {
    parts.push(t("stats.media_size", { size: formatMegabytes(stats.mediaBytes) }));
  }
  elements.statsEl.textContent = parts.join(" · ");
  elements.statsEl.classList.toggle("hidden", parts.length === 0);
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
    id: String(task.id || ""),
    sourceUrl: String(task.source_url || ""),
    displayName: typeof task.source_title === "string" ? task.source_title.trim() : "",
    baseStatus: String(task.status || ""),
    failureCode: parseFailureCode(task.failure_code),
    failureError: parseErrorMessage(task.error_message),
    queuePosition: parseQueuePosition(task.queue_position),
    enabledSteps,
    stepStatusByName,
    transcriptReady: Boolean(task.transcript_path),
    summaryExpected: enabledSteps.some((s) => s === "summarize_final" || s.startsWith("finalize:")),
    summaryReady:
      Boolean(task.summary_path) ||
      (Array.isArray(task.options && task.options.prompt_results) &&
        task.options.prompt_results.some((r) => r && r.status === "completed")),
    promptResults: Array.isArray(task.options && task.options.prompt_results)
      ? task.options.prompt_results
      : [],
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
  } else if (active === "summarize_final" || active.startsWith("finalize:")) {
    const finalStatus = runtime.stepStatusByName[active] || "";
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

function enterTitleEdit(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  if (!runtime || !elements) return;
  const isUpload = typeof runtime.sourceUrl === "string" && runtime.sourceUrl.startsWith("file://");
  const uploadName = isUpload ? runtime.sourceUrl.slice("file://".length) : "";
  const prefill = runtime.displayName || uploadName || runtime.sourceUrl || "";
  taskEl._editingTitle = true;
  elements.linkEl.classList.add("hidden");
  elements.editNameBtn.classList.add("hidden");
  if (elements.expiredEl) elements.expiredEl.classList.add("hidden");
  elements.nameEditWrap.classList.remove("hidden");
  elements.nameInput.value = prefill;
  elements.nameInput.disabled = false;
  elements.nameOkBtn.disabled = false;
  elements.nameInput.focus();
  elements.nameInput.select();
}

function cancelTitleEdit(taskEl) {
  const elements = taskEl._elements;
  if (!elements) return;
  taskEl._editingTitle = false;
  elements.nameEditWrap.classList.add("hidden");
  elements.linkEl.classList.remove("hidden");
  elements.editNameBtn.classList.remove("hidden");
  renderTaskTitle(taskEl);
}

async function commitTitleEdit(taskEl) {
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  if (!runtime || !elements) return;
  const value = elements.nameInput.value.trim();
  elements.nameOkBtn.disabled = true;
  elements.nameInput.disabled = true;
  try {
    const updated = await api(`/api/tasks/${encodeURIComponent(runtime.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: value }),
    });
    runtime.displayName = typeof updated.source_title === "string" ? updated.source_title.trim() : "";
    taskEl._editingTitle = false;
    elements.nameEditWrap.classList.add("hidden");
    elements.linkEl.classList.remove("hidden");
    elements.editNameBtn.classList.remove("hidden");
    renderTaskTitle(taskEl);
  } catch (err) {
    // Keep the editor open so the user can retry or cancel.
    elements.nameInput.disabled = false;
    elements.nameOkBtn.disabled = false;
    elements.nameInput.focus();
    console.error("rename failed", err);
  }
}

function renderTaskTitle(taskEl) {
  if (taskEl._editingTitle) {
    return;  // don't repaint the title while the user is editing it
  }
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;
  const hasName = Boolean(runtime.displayName);
  const isUpload = typeof runtime.sourceUrl === "string" && runtime.sourceUrl.startsWith("file://");
  const uploadName = isUpload ? runtime.sourceUrl.slice("file://".length) : "";
  const uploadExpired = isUpload && !runtime.mediaReady;
  const playerHref = isUpload ? buildPath(`/player/${encodeURIComponent(runtime.id)}`) : runtime.sourceUrl;
  elements.linkEl.textContent = hasName ? runtime.displayName : (isUpload ? uploadName : runtime.sourceUrl);
  if (uploadExpired) {
    elements.linkEl.removeAttribute("href");
    elements.linkEl.removeAttribute("target");
    elements.linkEl.removeAttribute("rel");
    elements.linkEl.classList.add("expired");
  } else {
    elements.linkEl.href = playerHref;
    elements.linkEl.classList.remove("expired");
    if (isUpload) {
      elements.linkEl.target = "_blank";
      elements.linkEl.rel = "noopener";
    } else {
      elements.linkEl.removeAttribute("target");
      elements.linkEl.removeAttribute("rel");
    }
  }
  if (elements.expiredEl) {
    elements.expiredEl.classList.toggle("hidden", !uploadExpired);
  }
  elements.sourceEl.textContent = isUpload ? uploadName : runtime.sourceUrl;
  elements.sourceEl.classList.toggle("hidden", !hasName);
}

function renderTaskRuntime(taskEl) {
  if (!taskEl || !taskEl._runtime || !taskEl._elements) {
    return;
  }
  const runtime = taskEl._runtime;
  const elements = taskEl._elements;

  renderTaskTitle(taskEl);
  renderTaskStats(taskEl);
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

  // Keep the results dropdown in sync as prompt_results grows on each poll,
  // but only when the results (summary) tab is the active one.
  if (getActiveTabName(taskEl) === "summary") {
    renderResultPromptSelect(taskEl);
  }

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
    const resultPromptBar = root.querySelector(".result-prompt-bar");
    const resultPromptSelect = root.querySelector(".result-prompt-select");
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
        openRestartFinalDialog(task);
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

    if (resultPromptSelect) {
      resultPromptSelect.addEventListener("change", () => {
        void loadSelectedResult(root, task.id);
      });
    }

    root._elements = {
      linkEl: root.querySelector(".task-link"),
      expiredEl: root.querySelector(".task-expired"),
      sourceEl: root.querySelector(".task-source"),
      statsEl: root.querySelector(".task-stats"),
      editNameBtn: root.querySelector(".task-edit-name-btn"),
      nameEditWrap: root.querySelector(".task-name-edit"),
      nameInput: root.querySelector(".task-name-input"),
      nameOkBtn: root.querySelector(".task-name-ok-btn"),
      nameCancelBtn: root.querySelector(".task-name-cancel-btn"),
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
      resultPromptBar,
      resultPromptSelect,
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
    const _els = root._elements;
    _els.editNameBtn.addEventListener("click", () => enterTitleEdit(root));
    _els.nameOkBtn.addEventListener("click", () => commitTitleEdit(root));
    _els.nameCancelBtn.addEventListener("click", () => cancelTitleEdit(root));
    _els.nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commitTitleEdit(root); }
      else if (e.key === "Escape") { e.preventDefault(); cancelTitleEdit(root); }
    });
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
    form.audio_only.disabled = true;
    form.audio_only.checked = false;
  } else {
    urlInput.classList.remove("hidden");
    urlInput.required = true;
    fileInput.classList.add("hidden");
    fileInput.required = false;
    form.audio_only.disabled = false;
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

let promptsCache = [];

function promptDisplayName(prompt) {
  if (prompt.source === "system") {
    const translated = t(prompt.name);
    return translated === prompt.name ? prompt.name : translated;
  }
  return prompt.name;
}

function setPromptPopoverOpen(container, open) {
  if (!container) {
    return;
  }
  const toggle = container.querySelector(".prompt-select-toggle");
  const popover = container.querySelector(".prompt-select-popover");
  if (!toggle || !popover) {
    return;
  }
  if (open && toggle.disabled) {
    return;
  }
  container.classList.toggle("open", open);
  popover.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

function togglePromptPopover(container) {
  const isOpen = container && container.classList.contains("open");
  setPromptPopoverOpen(container, !isOpen);
}

function updatePromptSelectSummary(container) {
  if (!container) {
    return;
  }
  const summary = container.querySelector(".prompt-select-summary");
  if (!summary) {
    return;
  }
  const checked = Array.from(
    container.querySelectorAll('input[type="checkbox"]:checked')
  );
  let text;
  if (checked.length === 0) {
    text = t("new_task.prompts_none");
  } else if (checked.length === 1) {
    const label = checked[0].closest(".prompt-row");
    const name = label && label.querySelector(".prompt-name");
    text = name ? name.textContent : t("new_task.prompts_count", { count: 1 });
  } else {
    text = t("new_task.prompts_count", { count: checked.length });
  }
  summary.textContent = text;
}

// Reusable, container-parameterized prompt multiselect renderer.
// Builds the toggle + popover into `container`; a checkbox is checked iff its
// {source,id} appears in `selectedRefs`. Used by the create-form selector and,
// in a later task, by the restart dialog with its own selection.
function renderPromptMultiselect(container, prompts, selectedRefs) {
  if (!container) {
    return;
  }
  const refs = Array.isArray(selectedRefs) ? selectedRefs : [];
  const list = Array.isArray(prompts) ? prompts : [];
  container.innerHTML = "";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "prompt-select-toggle";
  toggle.setAttribute("aria-haspopup", "true");
  toggle.setAttribute("aria-expanded", "false");

  const summary = document.createElement("span");
  summary.className = "prompt-select-summary";
  const caret = document.createElement("span");
  caret.className = "prompt-select-caret";
  caret.textContent = "▾";
  caret.setAttribute("aria-hidden", "true");
  toggle.append(summary, caret);

  const popover = document.createElement("div");
  popover.className = "prompt-select-popover";
  popover.hidden = true;

  for (const prompt of list) {
    const isSelected = refs.some(
      (r) => r.source === prompt.source && r.id === prompt.id
    );
    const label = document.createElement("label");
    label.className = "prompt-row";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = isSelected;
    checkbox.dataset.source = prompt.source;
    checkbox.dataset.id = prompt.id;

    const name = document.createElement("span");
    name.className = "prompt-name";
    name.textContent = promptDisplayName(prompt);

    const badge = document.createElement("span");
    badge.className = `prompt-badge prompt-badge-${prompt.source}`;
    badge.textContent = t(`prompt.badge.${prompt.source}`);

    label.append(checkbox, name, badge);
    popover.appendChild(label);
  }

  toggle.addEventListener("click", () => togglePromptPopover(container));
  popover.addEventListener("change", () => updatePromptSelectSummary(container));

  container.append(toggle, popover);
  updatePromptSelectSummary(container);
}

function renderPromptSelect(prompts) {
  if (!promptSelect) {
    return;
  }
  promptsCache = Array.isArray(prompts) ? prompts : [];
  renderPromptMultiselect(promptSelect, promptsCache, [
    { source: "system", id: "summary" },
  ]);
  syncSummaryToggle();
}

async function loadPrompts() {
  if (!promptSelect) {
    return;
  }
  try {
    const prompts = await api("/api/prompts");
    renderPromptSelect(prompts);
  } catch (err) {
    console.error("Failed to load prompts", err);
  }
}

function resetPromptSelection() {
  if (!promptSelect) {
    return;
  }
  promptSelect.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.checked = cb.dataset.source === "system" && cb.dataset.id === "summary";
  });
  updatePromptSelectSummary(promptSelect);
}

function getSelectedFrom(container) {
  if (!container) {
    return [];
  }
  return Array.from(
    container.querySelectorAll('input[type="checkbox"]:checked')
  ).map((cb) => ({ source: cb.dataset.source, id: cb.dataset.id }));
}

function getSelectedPrompts() {
  return promptSelect ? getSelectedFrom(promptSelect) : [];
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
    fd.append("prompts", JSON.stringify(getSelectedPrompts()));
    await uploadFileWithProgress(fd);
  } else {
    const payload = {
      url: form.url.value,
      language: form.language.value || null,
      audio_only: form.audio_only.checked,
      transcript: form.transcript.checked,
      prompts: getSelectedPrompts()
    };
    await api("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }
  form.reset();
  form.transcript.checked = true;
  resetPromptSelection();
  syncSummaryToggle();
  syncSourceType();
  await loadTasks();
}

function syncSummaryToggle() {
  if (!promptSelect) {
    return;
  }
  const disabled = !form.transcript.checked;
  promptSelect.classList.toggle("disabled", disabled);
  const toggle = promptSelect.querySelector(".prompt-select-toggle");
  if (toggle) {
    toggle.disabled = disabled;
  }
  promptSelect.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.disabled = disabled;
  });
  if (disabled) {
    setPromptPopoverOpen(promptSelect, false);
  }
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
        if (task.options && Array.isArray(task.options.prompt_results)) {
          runtime.promptResults = task.options.prompt_results;
        }
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
  // When a finalize step completes a new prompt_results entry has been written
  // server-side. Re-fetch the task so the results dropdown picks it up without
  // waiting for the next full poll.
  if (
    stepStatus === "completed" &&
    (stepName === "summarize_final" || stepName.startsWith("finalize:"))
  ) {
    void api(`/api/tasks/${taskId}`).then((task) => {
      if (taskEl._runtime === runtime && task && task.options) {
        runtime.promptResults = Array.isArray(task.options.prompt_results)
          ? task.options.prompt_results
          : runtime.promptResults;
        // A completed result means the Results tab can open even for a
        // custom-prompt-only task (no summary_path).
        if (runtime.promptResults.some((r) => r && r.status === "completed")) {
          runtime.summaryReady = true;
        }
        renderTaskRuntime(taskEl);
      }
    }).catch(() => {});
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

document.addEventListener("click", (event) => {
  document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
  // Close any open prompt-select popover whose container does not contain the click.
  document.querySelectorAll(".prompt-select.open").forEach((container) => {
    if (!container.contains(event.target)) {
      setPromptPopoverOpen(container, false);
    }
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll(".prompt-select.open").forEach((container) => {
      setPromptPopoverOpen(container, false);
    });
  }
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
document.getElementById("logout-btn")?.addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  window.location.href = "/";
});

// ---------- API tokens ----------

const tokensDialog = document.getElementById("tokens-dialog");
const tokensListEl = document.getElementById("tokens-list");
const tokensCreateForm = document.getElementById("tokens-create-form");
const tokensCreateNameInput = document.getElementById("tokens-create-name");
const tokensCreatedBanner = document.getElementById("tokens-created-banner");
const tokensRawValueEl = document.getElementById("tokens-raw-value");

function renderTokensList(tokens) {
  if (!tokensListEl) return;
  tokensListEl.innerHTML = "";
  if (!tokens.length) {
    const empty = document.createElement("p");
    empty.className = "tokens-empty";
    empty.textContent = t("tokens.empty");
    tokensListEl.appendChild(empty);
    return;
  }
  for (const tok of tokens) {
    const row = document.createElement("div");
    row.className = "tokens-row";

    const meta = document.createElement("div");
    meta.className = "tokens-meta";
    const name = document.createElement("span");
    name.className = "tokens-name";
    name.textContent = tok.name;
    const prefix = document.createElement("code");
    prefix.className = "mono tokens-prefix";
    prefix.textContent = `${tok.prefix}…`;
    meta.appendChild(name);
    meta.appendChild(prefix);
    row.appendChild(meta);

    const sub = document.createElement("div");
    sub.className = "tokens-sub";
    const created = new Date(tok.created_at).toLocaleString();
    const lastUsed = tok.last_used_at ? new Date(tok.last_used_at).toLocaleString() : t("tokens.never_used");
    sub.textContent = `${t("tokens.created")}: ${created} · ${t("tokens.last_used")}: ${lastUsed}`;
    row.appendChild(sub);

    const revokeBtn = document.createElement("button");
    revokeBtn.type = "button";
    revokeBtn.className = "btn-text ghost";
    revokeBtn.textContent = t("tokens.revoke");
    revokeBtn.addEventListener("click", async () => {
      if (!window.confirm(t("tokens.revoke_confirm"))) return;
      const resp = await fetch(buildPath(`/api/me/tokens/${encodeURIComponent(tok.id)}`), { method: "DELETE" });
      if (resp.ok) await refreshTokensList();
    });
    row.appendChild(revokeBtn);

    tokensListEl.appendChild(row);
  }
}

async function refreshTokensList() {
  const resp = await fetch(buildPath("/api/me/tokens"));
  if (!resp.ok) return;
  const tokens = await resp.json();
  renderTokensList(tokens);
}

function resetTokensDialog() {
  if (tokensCreatedBanner) tokensCreatedBanner.classList.add("hidden");
  if (tokensRawValueEl) tokensRawValueEl.textContent = "";
  if (tokensCreateNameInput) tokensCreateNameInput.value = "";
}

document.getElementById("tokens-btn")?.addEventListener("click", async () => {
  if (!tokensDialog) return;
  resetTokensDialog();
  await refreshTokensList();
  if (typeof tokensDialog.showModal === "function") {
    tokensDialog.showModal();
  } else {
    tokensDialog.setAttribute("open", "");
  }
});

document.getElementById("tokens-close-btn")?.addEventListener("click", () => {
  tokensDialog?.close();
});

tokensCreateForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = (tokensCreateNameInput?.value || "").trim();
  if (!name) return;
  const resp = await fetch(buildPath("/api/me/tokens"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) return;
  const created = await resp.json();
  if (tokensRawValueEl) tokensRawValueEl.textContent = created.token;
  tokensCreatedBanner?.classList.remove("hidden");
  if (tokensCreateNameInput) tokensCreateNameInput.value = "";
  await refreshTokensList();
});

document.getElementById("tokens-copy-btn")?.addEventListener("click", async () => {
  const value = tokensRawValueEl?.textContent || "";
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    // Fallback for browsers without async clipboard
    const range = document.createRange();
    range.selectNode(tokensRawValueEl);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
});

// ---------- Manage prompts ----------

const promptsDialog = document.getElementById("prompts-dialog");
const promptsListEl = document.getElementById("prompts-list");
const promptForm = document.getElementById("prompt-form");
const promptEditIdInput = document.getElementById("prompt-edit-id");
const promptNameInput = document.getElementById("prompt-name-input");
const promptBodyInput = document.getElementById("prompt-body-input");
const promptSubmitBtn = document.getElementById("prompt-submit-btn");
const promptCancelBtn = document.getElementById("prompt-cancel-btn");

function setPromptFormMode(editId) {
  if (promptEditIdInput) promptEditIdInput.value = editId || "";
  if (promptSubmitBtn) {
    promptSubmitBtn.textContent = editId
      ? t("prompts.manage.edit")
      : t("prompts.manage.create");
  }
  if (promptCancelBtn) promptCancelBtn.classList.toggle("hidden", !editId);
}

function resetPromptForm() {
  if (promptNameInput) promptNameInput.value = "";
  if (promptBodyInput) promptBodyInput.value = "";
  setPromptFormMode("");
}

function fillPromptForm({ name, body, editId }) {
  if (promptNameInput) promptNameInput.value = name || "";
  if (promptBodyInput) promptBodyInput.value = body || "";
  setPromptFormMode(editId || "");
  promptNameInput?.focus();
}

async function duplicatePrompt(prompt) {
  let body = "";
  let baseName = "";
  if (prompt.source === "system") {
    const detail = await api(`/api/prompts/system/${encodeURIComponent(prompt.id)}/text`);
    body = detail.system_prompt || "";
    baseName = promptDisplayName(prompt);
  } else {
    const detail = await api(`/api/prompts/${encodeURIComponent(prompt.id)}`);
    body = detail.system_prompt || "";
    baseName = detail.name;
  }
  fillPromptForm({
    name: `${baseName}${t("prompts.manage.copy_suffix")}`,
    body,
    editId: "",
  });
}

function renderPromptsList(prompts) {
  if (!promptsListEl) return;
  promptsListEl.innerHTML = "";
  for (const prompt of prompts) {
    const row = document.createElement("div");
    row.className = "tokens-row prompts-row";

    const meta = document.createElement("div");
    meta.className = "tokens-meta prompts-meta";
    const name = document.createElement("span");
    name.className = "tokens-name prompt-name";
    name.textContent = promptDisplayName(prompt);
    meta.appendChild(name);
    if (prompt.source === "system") {
      const badge = document.createElement("span");
      badge.className = "prompt-badge prompt-badge-system";
      badge.textContent = t("prompt.badge.system");
      meta.appendChild(badge);
    }
    row.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "prompts-actions";

    if (prompt.editable) {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn-text ghost";
      editBtn.textContent = t("prompts.manage.edit");
      editBtn.addEventListener("click", async () => {
        const detail = await api(`/api/prompts/${encodeURIComponent(prompt.id)}`);
        fillPromptForm({
          name: detail.name,
          body: detail.system_prompt || "",
          editId: detail.id,
        });
      });
      actions.appendChild(editBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn-text ghost";
      delBtn.textContent = t("prompts.manage.delete");
      delBtn.addEventListener("click", async () => {
        const resp = await fetch(buildPath(`/api/prompts/${encodeURIComponent(prompt.id)}`), { method: "DELETE" });
        if (resp.ok) {
          if (promptEditIdInput?.value === prompt.id) resetPromptForm();
          await refreshPromptsManager();
          await loadPrompts();
        }
      });
      actions.appendChild(delBtn);
    } else {
      const badge = document.createElement("span");
      badge.className = "prompts-readonly";
      badge.textContent = t("prompts.manage.system_readonly");
      actions.appendChild(badge);
    }

    const dupBtn = document.createElement("button");
    dupBtn.type = "button";
    dupBtn.className = "btn-text ghost";
    dupBtn.textContent = t("prompts.manage.duplicate");
    dupBtn.addEventListener("click", () => duplicatePrompt(prompt));
    actions.appendChild(dupBtn);

    row.appendChild(actions);
    promptsListEl.appendChild(row);
  }
}

async function refreshPromptsManager() {
  if (!promptsListEl) return;
  try {
    const prompts = await api("/api/prompts");
    renderPromptsList(prompts);
  } catch (err) {
    console.error("Failed to load prompts", err);
  }
}

document.getElementById("prompts-btn")?.addEventListener("click", async () => {
  if (!promptsDialog) return;
  resetPromptForm();
  await refreshPromptsManager();
  if (typeof promptsDialog.showModal === "function") {
    promptsDialog.showModal();
  } else {
    promptsDialog.setAttribute("open", "");
  }
});

document.getElementById("prompts-close-btn")?.addEventListener("click", () => {
  promptsDialog?.close();
});

promptCancelBtn?.addEventListener("click", () => {
  resetPromptForm();
});

promptForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = (promptNameInput?.value || "").trim();
  const systemPrompt = promptBodyInput?.value || "";
  if (!name || !systemPrompt.trim()) return;
  const editId = promptEditIdInput?.value || "";
  let resp;
  if (editId) {
    resp = await fetch(buildPath(`/api/prompts/${encodeURIComponent(editId)}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, system_prompt: systemPrompt }),
    });
  } else {
    resp = await fetch(buildPath("/api/prompts"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, system_prompt: systemPrompt }),
    });
  }
  if (!resp.ok) return;
  resetPromptForm();
  await refreshPromptsManager();
  await loadPrompts();
});

// ---------- Restart final dialog ----------

const restartFinalDialog = document.getElementById("restart-final-dialog");
const restartFinalSelect = document.getElementById("restart-final-select");
const restartFinalCloseBtn = document.getElementById("restart-final-close-btn");
const restartFinalSubmitBtn = document.getElementById("restart-final-submit-btn");
let restartFinalTaskId = null;

function updateRestartFinalSubmitState() {
  if (!restartFinalSubmitBtn) return;
  restartFinalSubmitBtn.disabled = getSelectedFrom(restartFinalSelect).length === 0;
}

async function openRestartFinalDialog(task) {
  if (!restartFinalDialog || !restartFinalSelect) {
    restartSummary(task.id, "final_only");
    return;
  }
  restartFinalTaskId = task.id;
  let prompts = [];
  try {
    prompts = await api("/api/prompts");
  } catch (err) {
    console.error("Failed to load prompts", err);
  }
  const selected =
    Array.isArray(task.options?.prompts) && task.options.prompts.length
      ? task.options.prompts
      : [{ source: "system", id: "summary" }];
  renderPromptMultiselect(restartFinalSelect, prompts, selected);
  updateRestartFinalSubmitState();
  if (typeof restartFinalDialog.showModal === "function") {
    restartFinalDialog.showModal();
  } else {
    restartFinalDialog.setAttribute("open", "");
  }
}

restartFinalSelect?.addEventListener("change", updateRestartFinalSubmitState);

restartFinalCloseBtn?.addEventListener("click", () => {
  restartFinalDialog?.close();
});

restartFinalSubmitBtn?.addEventListener("click", async () => {
  const prompts = getSelectedFrom(restartFinalSelect);
  if (!prompts.length || restartFinalTaskId == null) return;
  await apiBatchPost("/api/tasks/restart_summary", {
    task_ids: [restartFinalTaskId],
    mode: "final_only",
    prompts,
  });
  restartFinalDialog?.close();
  await loadTasks();
});

// ---------- Web Push ----------

const pushToggleBtn = document.getElementById("push-toggle-btn");
let pushConfig = null;

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
  return output;
}

function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

async function getPushSubscription() {
  try {
    const reg = await navigator.serviceWorker.ready;
    return await reg.pushManager.getSubscription();
  } catch {
    return null;
  }
}

function setPushButtonState(state) {
  if (!pushToggleBtn) return;
  const label =
    state === "subscribed"
      ? t("action.disable_notifications")
      : t("action.enable_notifications");
  pushToggleBtn.title = label;
  pushToggleBtn.setAttribute("aria-label", label);
  pushToggleBtn.classList.toggle("push-active", state === "subscribed");
  pushToggleBtn.disabled = state === "pending";
}

async function loadPushConfig() {
  if (!pushToggleBtn) return;
  if (!pushSupported()) return;
  try {
    pushConfig = await api("/api/push/config");
  } catch {
    return;
  }
  if (!pushConfig || !pushConfig.enabled) return;
  pushToggleBtn.classList.remove("hidden");
  const sub = await getPushSubscription();
  setPushButtonState(sub ? "subscribed" : "idle");
}

async function subscribeToPush() {
  if (!pushConfig || !pushConfig.public_key) {
    window.alert("Push is not configured on the server.");
    return;
  }
  if (Notification.permission === "denied") {
    window.alert("Notifications are blocked for this site in the browser settings.");
    return;
  }
  setPushButtonState("pending");
  try {
    if (Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        setPushButtonState("idle");
        return;
      }
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(pushConfig.public_key),
    });
    const json = sub.toJSON();
    await api("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: sub.endpoint,
        p256dh: json.keys && json.keys.p256dh,
        auth: json.keys && json.keys.auth,
        user_agent: navigator.userAgent,
      }),
    });
    setPushButtonState("subscribed");
  } catch (err) {
    console.error("push subscribe failed", err);
    setPushButtonState("idle");
  }
}

async function unsubscribeFromPush() {
  setPushButtonState("pending");
  try {
    const sub = await getPushSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      }).catch(() => {});
      await sub.unsubscribe();
    }
  } finally {
    setPushButtonState("idle");
  }
}

async function togglePush() {
  const sub = await getPushSubscription();
  if (sub) {
    await unsubscribeFromPush();
  } else {
    await subscribeToPush();
  }
}

if (pushToggleBtn) {
  pushToggleBtn.addEventListener("click", togglePush);
}

// ---------- Share target: pending file handoff from service worker ----------

async function applyPendingSharedFileIfAny() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("share_pending") !== "file") return;
  // Drop the marker immediately so a reload doesn't retry.
  const clean = window.location.pathname + window.location.hash;
  window.history.replaceState({}, "", clean);
  try {
    const resp = await fetch("/_share_inbox");
    if (!resp.ok) return;
    const filenameHeader = resp.headers.get("X-Share-Filename") || "";
    const filename = filenameHeader ? decodeURIComponent(filenameHeader) : "shared";
    const blob = await resp.blob();
    const file = new File([blob], filename, { type: blob.type || "application/octet-stream" });
    const fileInput = document.getElementById("file-input");
    const fileRadio = document.getElementById("source-type-file");
    if (fileRadio && !fileRadio.checked) {
      fileRadio.checked = true;
      syncSourceType();
    }
    if (fileInput) {
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      fileInput.focus();
    }
  } catch (err) {
    console.warn("shared file handoff failed", err);
  }
}

// ---------- Notification click from SW ----------

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (event) => {
    const msg = event.data || {};
    if (msg.type === "notification_click" && msg.task_id) {
      const row = document.querySelector(`[data-task-id="${msg.task_id}"]`);
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        row.classList.add("flash");
        setTimeout(() => row.classList.remove("flash"), 2000);
      }
    }
  });
}

function extractUrlFromSharePayload() {
  // Android share sheets (especially YouTube) often deliver the URL inside
  // `text` rather than `url`. Scan all forwarded fields and pick the first
  // http(s) URL we find.
  const params = new URLSearchParams(window.location.search);
  const candidates = [
    params.get("share_url"),
    params.get("share_text"),
    params.get("share_title"),
  ].filter((v) => typeof v === "string" && v.length > 0);
  for (const candidate of candidates) {
    const match = candidate.match(/https?:\/\/\S+/);
    if (match) return match[0];
  }
  return null;
}

function applySharedUrlIfAny() {
  const shared = extractUrlFromSharePayload();
  if (!shared) return;
  const urlInput = document.getElementById("url");
  const urlRadio = document.getElementById("source-type-url");
  if (urlRadio && !urlRadio.checked) {
    urlRadio.checked = true;
    syncSourceType();
  }
  if (urlInput) {
    urlInput.value = shared;
    urlInput.focus();
  }
  // Clean the query string so reloads don't keep re-applying it.
  const clean = window.location.pathname + window.location.hash;
  window.history.replaceState({}, "", clean);
}

async function bootstrap() {
  await ensureI18nLoaded();
  applyI18nToPage();
  setVersionLabel(BUILD_VERSION);
  syncSummaryToggle();
  syncSourceType();
  applySharedUrlIfAny();
  await applyPendingSharedFileIfAny();
  await refreshAll();
  await loadPrompts();
  await loadPushConfig();
}

void bootstrap();

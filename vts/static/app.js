const ICON_EDIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.58z"/></svg>';
const ICON_DELETE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';
const ICON_DUPLICATE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_MAKE_DEFAULT = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l3 6.5 7 .9-5 4.8 1.3 7L12 17.8 5.4 21.2 6.7 14.2 1.7 9.4l7-.9z"/></svg>';
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
const presetSelect = document.getElementById("preset-select");
const presetSaveBtn = document.getElementById("preset-save-btn");
const presetDanglingHint = document.getElementById("preset-dangling-hint");
const presetResaveBtn = document.getElementById("preset-resave-btn");
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
  // Runs unconditionally (self-gates on options.diarize internally, same as the
  // server step) — always present in task.steps, so it must be in the static
  // head, not the options-gated summary tail.
  "diarize",
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
  "diarize",
  "merge_transcript"
];
// Back-compat alias kept for any legacy references (full static summary path).
const DAG_STEPS = [...DAG_HEAD, "summarize_final"];
// Relative per-step weights (in seconds) — medians recomputed over completed
// pipeline runs on 2026-06-28 (n=56–64 runs per step).
const STEP_WEIGHT_SECONDS = {
  download: 5.5,
  extract_audio: 2.0,
  trim_initial_silence: 0.3,
  segment_audio: 1.2,
  detect_language: 2.6,
  transcribe_segments: 174.8,
  // No completed-run samples yet (feature just wired into the DAG); a small
  // placeholder keeps the progress bar sane until server-side weights accrue
  // real medians (see getStepWeight's serverStepWeights fallback chain).
  diarize: 1.0,
  merge_transcript: 0.1,
  prepare_llama_model: 6.3,
  prepare_summary_chunks: 0.1,
  summarize_windows: 74.8
};
// Fallback = median summarize_final over completed runs (recomputed 2026-06-28).
const FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS = 514.4;

let serverStepWeights = null;
let serverFinalFallback = null;
let uploadConfig = null;

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
  const name = String(stepName || "");
  // Dynamic per-prompt finalize steps ("finalize:<source>:<id>") have no static
  // i18n key. Render a human label with the resolved prompt name instead of the
  // raw "finalize:user:<uuid>".
  if (name.startsWith("finalize:")) {
    const rest = name.slice("finalize:".length);
    const idx = rest.indexOf(":");
    const source = idx >= 0 ? rest.slice(0, idx) : rest;
    const id = idx >= 0 ? rest.slice(idx + 1) : "";
    if (source && id) {
      return t("step.finalize_prompt", { name: aboutResolvePromptName(source, id) });
    }
  }
  const key = `steps.${name}`;
  const translated = t(key);
  return translated === key ? name : translated;
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
    const text = t(el.getAttribute("data-i18n-title") || "");
    // Render through the styled bubble, not the browser's native tooltip: the
    // native one never appears on touch (it needs hover), which is why the
    // bubble exists. `title` stays as the pre-JS/assistive fallback, but is
    // dropped once the bubble is in place so the two don't both show on hover.
    el.setAttribute("data-tooltip", text);
    el.removeAttribute("title");
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
  const hasWindows = Number.isFinite(summaryTotal) && summaryTotal > 1;
  const perWindow = (serverStepWeights && Number.isFinite(Number(serverStepWeights.summarize_windows)))
    ? Number(serverStepWeights.summarize_windows)
    : STEP_WEIGHT_SECONDS.summarize_windows;
  if (hasWindows) {
    return perWindow;
  }
  return Number.isFinite(serverFinalFallback) ? serverFinalFallback : FINAL_SUMMARY_WEIGHT_FALLBACK_SECONDS;
}

function getStepWeight(runtime, stepName) {
  if (stepName === "summarize_final" || stepName.startsWith("finalize:")) {
    return estimateFinalSummaryWeight(runtime);
  }
  const serverVal = serverStepWeights ? Number(serverStepWeights[stepName]) : NaN;
  if (Number.isFinite(serverVal) && serverVal > 0) {
    return serverVal;
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
  if (elements.statsTextEl) {
    elements.statsTextEl.textContent = parts.join(" · ");
  }
  elements.statsEl.classList.toggle("hidden", parts.length === 0);
}

// Shared formatter for the completed-run numbers (total time + char counts).
// Used by the About-task dialog. Returns localized display strings.
function formatResultStats(runtime) {
  const stats = runtime.stats || {};
  return {
    time: formatMetricDuration(stats.processingSeconds),
    raw: formatMetricChars(stats.transcriptChars),
    processed: formatMetricChars(stats.redactedChars),
    summary: formatMetricChars(stats.summaryChars)
  };
}

function resolveTaskMessage(runtime) {
  // Card message line now carries ONLY the failure text. The success stats
  // moved into the About-task dialog (formatResultStats).
  return resolveFailureMessage(runtime);
}

const taskAboutDialog = document.getElementById("task-about-dialog");

// Resolve a {source,id} prompt ref to a display-name-bearing object. Prefers a
// name carried in prompt_results, else looks the user prompt up in promptsCache
// (so a still-running task whose prompt_results aren't populated yet shows the
// human name, not a GUID), else falls back to the id.
function aboutResolvePromptName(source, id) {
  const cached = promptsCache.find((p) => p.source === source && p.id === id);
  const name = cached ? cached.name : id;
  return promptDisplayName({ source, id, name });
}

function aboutPromptRefs(options) {
  // Prefer prompt_results (carries names); fall back to selected refs.
  const results = Array.isArray(options.prompt_results) ? options.prompt_results : null;
  if (results && results.length) {
    return results.map((r) => ({
      source: r.source,
      id: r.id,
      name: r.name || aboutResolvePromptName(r.source, r.id),
    }));
  }
  return selectedPromptRefs(options).map((r) => ({
    source: r.source,
    id: r.id,
    name: aboutResolvePromptName(r.source, r.id),
  }));
}

function aboutPromptNames(options) {
  return aboutPromptRefs(options).map((r) => promptDisplayName(r));
}

function aboutPromptTimings(task) {
  // One row per selected prompt: display name + finalize-step duration.
  const options = task.options || {};
  const stepByName = {};
  (task.steps || []).forEach((s) => { if (s && s.name) stepByName[s.name] = s; });
  const refs = aboutPromptRefs(options);
  return refs.map((ref) => {
    const step = stepByName[finalizeStepName(ref.source, ref.id)];
    const start = step ? parseIsoMs(step.started_at) : null;
    const end = step ? parseIsoMs(step.finished_at) : null;
    const duration = (start !== null && end !== null && end >= start)
      ? formatDuration((end - start) / 1000)
      : "—";
    return { name: promptDisplayName(ref), duration };
  });
}

// Render a boolean value as an icon (✓ for yes, — for no) into `el`, with an
// accessible label so screen readers still hear yes/no.
const ABOUT_ICON_YES = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>';
const ABOUT_ICON_NO = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 12h12"/></svg>';

function setAboutBool(el, value) {
  if (!el) {
    return;
  }
  el.classList.add("about-bool");
  el.classList.toggle("is-yes", value);
  el.classList.toggle("is-no", !value);
  el.innerHTML = value ? ABOUT_ICON_YES : ABOUT_ICON_NO;
  el.setAttribute("aria-label", value ? t("about.yes") : t("about.no"));
  el.setAttribute("title", value ? t("about.yes") : t("about.no"));
}

function renderTaskAboutDialog(task) {
  if (!taskAboutDialog) {
    return;
  }
  const options = task.options || {};
  const runtime = { stats: parseTaskStats(task), baseStatus: String(task.status || "") };
  const q = (sel) => taskAboutDialog.querySelector(sel);

  // Title as a clickable link, mirroring the card's .task-link behavior:
  // uploads link to the local player (or are unlinked when media expired),
  // everything else links to its source URL.
  const sourceUrl = task.source_url || "";
  const isUpload = sourceUrl.startsWith("file://");
  const uploadName = isUpload ? sourceUrl.slice("file://".length) : "";
  const titleEl = q(".about-source-title");
  titleEl.textContent = task.source_title || (isUpload ? uploadName : sourceUrl);
  const mediaReady = Boolean(task.media_path);
  const titleHref = isUpload
    ? (mediaReady ? buildPath(`/player/${encodeURIComponent(task.id)}`) : "")
    : sourceUrl;
  if (titleHref) {
    titleEl.href = titleHref;
    if (isUpload) {
      titleEl.target = "_blank";
      titleEl.rel = "noopener";
    } else {
      titleEl.target = "_blank";
      titleEl.rel = "noopener noreferrer";
    }
  } else {
    titleEl.removeAttribute("href");
  }
  q(".about-source-url").textContent = sourceUrl;
  q(".about-created").textContent = task.created_at
    ? new Date(task.created_at).toLocaleString()
    : "";

  q(".about-language").textContent = options.language || t("about.language_auto");
  setAboutBool(q(".about-audio-only"), Boolean(options.audio_only));
  setAboutBool(q(".about-transcript"), options.transcript !== false);
  q(".about-prompts").textContent = aboutPromptNames(options).join(", ") || "—";

  const completed = String(task.status || "") === "completed";
  const resultsSection = q(".about-results-section");
  resultsSection.classList.toggle("hidden", !completed);
  if (completed) {
    const fmt = formatResultStats(runtime);
    q(".about-total-time").textContent = fmt.time;
    q(".about-raw-chars").textContent = fmt.raw;
    q(".about-processed-chars").textContent = fmt.processed;
    q(".about-summary-chars").textContent = fmt.summary;
    const tbody = q(".about-prompt-timings");
    tbody.innerHTML = "";
    aboutPromptTimings(task).forEach((row) => {
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      nameTd.textContent = row.name;
      const durTd = document.createElement("td");
      durTd.textContent = row.duration;
      tr.appendChild(nameTd);
      tr.appendChild(durTd);
      tbody.appendChild(tr);
    });
  }
}

// Populate promptsCache if it hasn't been loaded yet, so user-prompt names
// resolve in the About dialog even when the create form was never opened.
async function ensurePromptsCache() {
  if (promptsCache.length) {
    return;
  }
  try {
    const prompts = await api("/api/prompts");
    promptsCache = Array.isArray(prompts) ? prompts : [];
  } catch (err) {
    console.error("Failed to load prompts for About dialog", err);
  }
}

async function openTaskAboutDialog(task) {
  if (!taskAboutDialog) {
    return;
  }
  await ensurePromptsCache();
  renderTaskAboutDialog(task);
  if (typeof taskAboutDialog.showModal === "function") {
    taskAboutDialog.showModal();
  } else {
    taskAboutDialog.setAttribute("open", "");
  }
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
    queue: task.queue || null,
    capabilities: task.capabilities || {},
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
  // A completed task is terminal: the last enabled step is the active one,
  // regardless of any leftover download flags from live SSE events watched
  // during the run (hasVideo/hasAudio persist on runtime and would otherwise
  // resolve back to "download" -> "step 1 of N" on the post-completion render).
  // specific status, not a group: `failed` must resolve to failedStepName below,
  // so isFinished() here would mis-resolve failed/canceled/archived tasks.
  if (runtime.baseStatus === "completed" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[runtime.enabledSteps.length - 1];
  }
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
  // specific status, not a group: resolving the first incomplete step is a
  // running-only fallback. A `waiting` task must fall through to "" so the
  // overall bar counts only finished-step weight (vts-qzl); isActive() here
  // would add partial active-step weight and change what `waiting` renders.
  if (runtime.baseStatus === "running") {
    const firstIncomplete = runtime.enabledSteps.find(
      (step) => !isStepFinishedStatus(runtime.stepStatusByName[step] || "")
    );
    if (firstIncomplete) {
      return firstIncomplete;
    }
  }
  // specific status, not a group: isPending() also covers `waiting`, which must
  // NOT snap back to step 1 (vts-qzl).
  if (runtime.baseStatus === "queued" && runtime.enabledSteps.length > 0) {
    return runtime.enabledSteps[0];
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
  // Each branch below renders a DIFFERENT string ("100%" / failed / queue
  // position), so these are per-status renders, not a group question:
  // isFinished()/isPending() would collapse distinct outputs.
  if (runtime.baseStatus === "completed") {
    return { value: 1, indeterminate: false, text: "100%" };
  }
  if (runtime.baseStatus === "failed") {
    return { value: 1, indeterminate: false, text: t("progress.failed") };
  }
  // specific status, not a group: `waiting` (also pending) is handled below.
  if (runtime.baseStatus === "queued") {
    if (runtime.queuePosition) {
      return { value: 0, indeterminate: false, text: t("progress.queue_pos", { position: runtime.queuePosition }) };
    }
    return { value: 0, indeterminate: false, text: t("progress.queued") };
  }

  const active = resolveActiveStep(runtime);
  // `waiting` = partially processed, the active step is queued in a lane for a
  // slot. Show real progress (completed steps count) with a "waiting: <lane>"
  // label on the active step, NOT a queued 0% (regression from VOS-85).
  if (runtime.baseStatus === "waiting") {
    const laneText = runtime.queue
      ? t("progress.waiting_lane", { queue: t(`queue.${runtime.queue}`) })
      : t("status.waiting");
    return { value: 0.05, indeterminate: true, text: laneText };
  }
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
  // Per-status renders, not a group question — see computeLocalStepProgress.
  if (runtime.baseStatus === "completed") {
    return { value: 1, indeterminate: false, text: "100%" };
  }
  if (runtime.baseStatus === "failed") {
    return { value: 1, indeterminate: false, text: t("progress.failed") };
  }
  // specific status, not a group: `waiting` must fall through to the per-step
  // computation below (vts-qzl), so isPending() here would regress it.
  if (runtime.baseStatus === "queued") {
    if (runtime.queuePosition) {
      return { value: 0, indeterminate: false, text: t("progress.queue_pos", { position: runtime.queuePosition }) };
    }
    return { value: 0, indeterminate: false, text: t("progress.queued") };
  }

  // `waiting` falls through to the normal per-step computation below so the
  // overall bar reflects the steps already completed, not a queued 0%.
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

function setTaskStatusAppearance(statusEl, status, queuePosition = null, queue = null) {
  if (status === "waiting") {
    if (queue && queuePosition) {
      statusEl.textContent = t("status.waiting_pos", { queue: t(`queue.${queue}`), position: queuePosition });
    } else if (queue) {
      // Lane known but position not yet fetched (SSE waiting event carries the
      // lane, the per-lane position arrives on the next task-list refresh).
      statusEl.textContent = t("progress.waiting_lane", { queue: t(`queue.${queue}`) });
    } else {
      statusEl.textContent = t("status.waiting");
    }
  } else if (status === "queued" && queuePosition) {
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
  setTaskStatusAppearance(elements.statusEl, runtime.baseStatus, runtime.queuePosition, runtime.queue);
  const canPause = statusPred.canPause(runtime.baseStatus);
  const canResume = statusPred.canResume(runtime.baseStatus);
  const canRestartSummary = statusPred.canRestartSummary(runtime);
  const canRestartFinalSummary = statusPred.canRestartFinalSummary(runtime);
  const canArchive = statusPred.canArchive(runtime.baseStatus);
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

  // specific status, not a group: only a running task's elapsed timer ticks;
  // a waiting task is not executing, so it must keep a blank runtime.
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

  // specific status, not a group: step stopwatch runs only while executing.
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

    pauseBtn.setAttribute("data-tooltip", t("action.pause"));
    pauseBtn.setAttribute("aria-label", t("action.pause"));
    resumeBtn.setAttribute("data-tooltip", t("action.resume"));
    resumeBtn.setAttribute("aria-label", t("action.resume"));
    if (restartSummaryBtn) {
      restartSummaryBtn.setAttribute("data-tooltip", t("action.restart_summary"));
      restartSummaryBtn.setAttribute("aria-label", t("action.restart_summary"));
    }
    if (restartSummaryFullBtn) {
      restartSummaryFullBtn.textContent = t("action.restart_summary_full");
      restartSummaryFullBtn.setAttribute("data-tooltip", t("action.restart_summary_full_tooltip"));
    }
    if (restartSummaryFinalBtn) {
      restartSummaryFinalBtn.textContent = t("action.restart_summary_final");
      restartSummaryFinalBtn.setAttribute("data-tooltip", t("action.restart_summary_final_tooltip"));
    }
    if (downloadMediaBtn) {
      downloadMediaBtn.setAttribute("data-tooltip", t("action.download_media"));
      downloadMediaBtn.setAttribute("aria-label", t("action.download_media"));
    }
    if (archiveBtn) {
      archiveBtn.setAttribute("data-tooltip", t("action.archive"));
      archiveBtn.setAttribute("aria-label", t("action.archive"));
    }
    deleteBtn.setAttribute("data-tooltip", t("action.delete"));
    deleteBtn.setAttribute("aria-label", t("action.delete"));
    toggleBtn.setAttribute("data-tooltip", t("action.expand"));
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
      statsTextEl: root.querySelector(".task-stats-text"),
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
    if (root._elements && root._elements.statsEl) {
      root._elements.statsEl.addEventListener("click", () => openTaskAboutDialog(task));
    }
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
  // `audio_only` only means anything to yt-dlp, which never runs for an uploaded
  // file, so the pill is hidden for the File source. The checkbox keeps its
  // value on purpose: presets stay clean and the choice survives switching back
  // to a URL. The flag is dropped at the upload boundary instead.
  const audioOnlyPill = document.getElementById("audio-only-pill");
  if (isFile) {
    urlInput.classList.add("hidden");
    urlInput.required = false;
    fileInput.classList.remove("hidden");
    fileInput.required = true;
    if (audioOnlyPill) audioOnlyPill.classList.add("hidden");
  } else {
    urlInput.classList.remove("hidden");
    urlInput.required = true;
    fileInput.classList.add("hidden");
    fileInput.required = false;
    if (audioOnlyPill) audioOnlyPill.classList.remove("hidden");
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

async function uploadFileChunked(file, fields) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;
  const setProgress = (r) => { if (fill) fill.style.strokeDashoffset = circumference * (1 - r); };

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  setProgress(0); // determinate from the start

  try {
    const init = await api("/api/uploads/init", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        total_size: file.size,
        language: fields.language || null,
        audio_only: fields.audio_only,
        transcript: fields.transcript,
        prompts: fields.prompts,
        display_name: fields.display_name || null,
      }),
      headers: {
        "Content-Type": "application/json",
        "X-Forwarded-User": state.authUser,
      },
    });
    const uploadId = init.upload_id;
    const chunkSize = init.chunk_size || 8388608;
    let offset = 0;
    while (offset < file.size) {
      const slice = file.slice(offset, Math.min(offset + chunkSize, file.size));
      const buf = await slice.arrayBuffer();
      let resp;
      try {
        resp = await api(`/api/uploads/${uploadId}?offset=${offset}`, {
          method: "PATCH",
          body: buf,
          headers: {
            "Content-Type": "application/offset+octet-stream",
            "X-Forwarded-User": state.authUser,
          },
        });
      } catch (err) {
        // On offset conflict or transient error, re-sync from the server.
        const off = await api(`/api/uploads/${uploadId}/offset`, {
          headers: { "X-Forwarded-User": state.authUser },
        });
        offset = off.received;
        setProgress(offset / file.size);
        continue;
      }
      offset = resp.received;
      setProgress(offset / file.size);
    }
    await api(`/api/uploads/${uploadId}/finalize`, {
      method: "POST",
      headers: { "X-Forwarded-User": state.authUser },
    });
    setProgress(1);
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
  }
}

let promptsCache = [];

function promptDisplayName(prompt) {
  if (prompt.source === "system") {
    const key = `prompt.system.${prompt.id}`;
    const translated = t(key);
    return translated === key ? prompt.name : translated;
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
function buildPromptRow(prompt, refs) {
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
  return label;
}

function renderPromptMultiselect(container, prompts, selectedRefs, opts = {}) {
  if (!container) {
    return;
  }
  const refs = Array.isArray(selectedRefs) ? selectedRefs : [];
  const list = Array.isArray(prompts) ? prompts : [];
  container.innerHTML = "";

  // Flat mode: append rows directly into the container as an always-visible
  // scrollable list — no toggle, no popover, no summary (used by the restart
  // dialog where there is plenty of vertical room).
  if (opts.flat === true) {
    for (const prompt of list) {
      container.appendChild(buildPromptRow(prompt, refs));
    }
    return;
  }

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
    popover.appendChild(buildPromptRow(prompt, refs));
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

// ---- Presets (create-form dropdown + apply + save) --------------------------

let presetsCache = [];
let selectedPresetRef = null; // {source, id} or null
let presetDirty = false;
let danglingResaveRefs = null; // filtered prompts to PATCH when the hint is used

function presetRefStr(ref) {
  return ref ? `${ref.source}:${ref.id}` : "";
}

function presetLabel(preset) {
  if (preset.source === "system") {
    const key = `preset.system.${preset.id}`;
    const translated = t(key);
    return translated === key ? preset.name : translated;
  }
  return preset.name;
}

function findPreset(ref) {
  if (!ref) {
    return null;
  }
  return (
    presetsCache.find((p) => p.source === ref.source && p.id === ref.id) || null
  );
}

// Returns the current four-field options object from the form controls.
function currentFormOptions() {
  return {
    language: form.language.value || "",
    audio_only: !!form.audio_only.checked,
    transcript: !!form.transcript.checked,
    prompts: getSelectedPrompts(),
  };
}

function promptRefsEqual(a, b) {
  const norm = (list) =>
    (Array.isArray(list) ? list : [])
      .map((r) => `${r.source}:${r.id}`)
      .sort();
  const sa = norm(a);
  const sb = norm(b);
  return sa.length === sb.length && sa.every((v, i) => v === sb[i]);
}

function optionsEqual(a, b) {
  const oa = a || {};
  const ob = b || {};
  return (
    (oa.language || "") === (ob.language || "") &&
    !!oa.audio_only === !!ob.audio_only &&
    !!oa.transcript === !!ob.transcript &&
    promptRefsEqual(oa.prompts, ob.prompts)
  );
}

// Drop user-prompt refs that are no longer present in the loaded prompts list.
// System refs are always kept (system prompts are always valid). Returns
// { filtered, dangling } where dangling is true if any ref was dropped.
function filterDanglingPrompts(refs) {
  const list = Array.isArray(refs) ? refs : [];
  const filtered = list.filter((r) => {
    if (r.source === "system") {
      return true;
    }
    return promptsCache.some((p) => p.source === r.source && p.id === r.id);
  });
  return { filtered, dangling: filtered.length !== list.length };
}

function applyPresetOptions(options) {
  const opts = options || {};
  form.language.value = opts.language || "";
  form.audio_only.checked = !!opts.audio_only;
  form.transcript.checked = !!opts.transcript;
  const { filtered, dangling } = filterDanglingPrompts(opts.prompts);
  if (promptSelect) {
    renderPromptMultiselect(promptSelect, promptsCache, filtered);
  }
  syncSummaryToggle();
  return dangling;
}

function updatePresetSaveBtn() {
  if (!presetSaveBtn) {
    return;
  }
  const preset = findPreset(selectedPresetRef);
  const isUserPreset = preset && preset.source === "user" && preset.editable;
  if (preset && presetDirty && isUserPreset) {
    presetSaveBtn.textContent = t("preset.save_changes");
    presetSaveBtn.dataset.mode = "patch";
  } else {
    presetSaveBtn.textContent = t("preset.save_as");
    presetSaveBtn.dataset.mode = "create";
  }
}

function recomputePresetDirty() {
  const preset = findPreset(selectedPresetRef);
  presetDirty = preset ? !optionsEqual(currentFormOptions(), preset.options) : false;
  updatePresetSaveBtn();
}

function showDanglingHint(show) {
  if (!presetDanglingHint) {
    return;
  }
  presetDanglingHint.hidden = !show;
}

// Apply a preset by ref: select it in the dropdown, fill the form, set up the
// dangling hint, and reset dirty state (a freshly-applied preset is clean).
function applyPresetById(ref) {
  const preset = findPreset(ref);
  if (!preset) {
    selectedPresetRef = null;
    showDanglingHint(false);
    presetDirty = false;
    updatePresetSaveBtn();
    return;
  }
  selectedPresetRef = { source: preset.source, id: preset.id };
  if (presetSelect) {
    presetSelect.value = presetRefStr(selectedPresetRef);
  }
  const dangling = applyPresetOptions(preset.options);
  if (dangling && preset.source === "user" && preset.editable) {
    danglingResaveRefs = filterDanglingPrompts(preset.options.prompts).filtered;
    showDanglingHint(true);
  } else {
    danglingResaveRefs = null;
    showDanglingHint(false);
  }
  presetDirty = false;
  updatePresetSaveBtn();
}

function populatePresetSelect() {
  if (!presetSelect) {
    return;
  }
  presetSelect.innerHTML = "";
  for (const preset of presetsCache) {
    const opt = document.createElement("option");
    opt.value = presetRefStr({ source: preset.source, id: preset.id });
    opt.textContent = presetLabel(preset);
    presetSelect.appendChild(opt);
  }
}

async function loadPresets() {
  if (!presetSelect) {
    return;
  }
  try {
    const presets = await api("/api/presets");
    presetsCache = Array.isArray(presets) ? presets : [];
    populatePresetSelect();
    let defaultRef = null;
    try {
      defaultRef = await api("/api/me/default_preset");
    } catch (err) {
      console.error("Failed to load default preset", err);
    }
    const ref =
      findPreset(defaultRef) ? defaultRef : presetsCache[0] || null;
    if (ref) {
      applyPresetById({ source: ref.source, id: ref.id });
    } else {
      updatePresetSaveBtn();
    }
  } catch (err) {
    console.error("Failed to load presets", err);
  }
}

async function savePresetClicked() {
  const mode = presetSaveBtn ? presetSaveBtn.dataset.mode : "create";
  const preset = findPreset(selectedPresetRef);
  if (mode === "patch" && preset) {
    try {
      await api(`/api/presets/${preset.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ options: currentFormOptions() }),
      });
    } catch (err) {
      console.error("Failed to save preset changes", err);
      return;
    }
    const keep = { source: preset.source, id: preset.id };
    await loadPresets();
    applyPresetById(keep);
    return;
  }
  // create mode
  const name = window.prompt(t("preset.name_prompt"));
  if (!name) {
    return;
  }
  let created;
  try {
    created = await api("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, options: currentFormOptions() }),
    });
  } catch (err) {
    console.error("Failed to create preset", err);
    return;
  }
  await loadPresets();
  if (created && created.id) {
    applyPresetById({ source: created.source || "user", id: created.id });
  }
}

async function resavePresetClicked() {
  const preset = findPreset(selectedPresetRef);
  if (!preset || !danglingResaveRefs) {
    showDanglingHint(false);
    return;
  }
  const options = { ...(preset.options || {}), prompts: danglingResaveRefs };
  try {
    await api(`/api/presets/${preset.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ options }),
    });
  } catch (err) {
    console.error("Failed to re-save preset", err);
    return;
  }
  const keep = { source: preset.source, id: preset.id };
  await loadPresets();
  applyPresetById(keep);
  showDanglingHint(false);
}

if (presetSelect) {
  presetSelect.addEventListener("change", () => {
    const [source, id] = (presetSelect.value || "").split(":");
    applyPresetById({ source, id });
  });
}
if (presetSaveBtn) {
  presetSaveBtn.addEventListener("click", () => {
    void savePresetClicked();
  });
}
if (presetResaveBtn) {
  presetResaveBtn.addEventListener("click", () => {
    void resavePresetClicked();
  });
}
form.language.addEventListener("change", recomputePresetDirty);
form.audio_only.addEventListener("change", recomputePresetDirty);
form.transcript.addEventListener("change", recomputePresetDirty);
if (promptSelect) {
  promptSelect.addEventListener("change", recomputePresetDirty);
}

const taskFormError = document.getElementById("task-form-error");

function showTaskFormError(message) {
  if (!taskFormError) return;
  taskFormError.textContent = message;
  taskFormError.classList.remove("hidden");
}

function clearTaskFormError() {
  if (!taskFormError) return;
  taskFormError.textContent = "";
  taskFormError.classList.add("hidden");
}

// Chrome throws these DOMExceptions when a File selected earlier can no longer
// be read: the file was modified/moved/deleted after selection, or it is an
// unsynced cloud placeholder (OneDrive/Google Drive "files on demand").
function isFileReadError(err) {
  return err instanceof DOMException
    && ["NotReadableError", "NotFoundError", "SecurityError"].includes(err.name);
}

async function createTask(event) {
  event.preventDefault();
  clearTaskFormError();
  const isFile = getSourceType() === "file";
  const fileInput = document.getElementById("file-input");
  try {
    if (isFile && fileInput) {
      const file = fileInput.files[0];
      // Probe one byte before starting: a stale file reference fails here with
      // a clear message instead of mid-upload (covers the single-shot XHR path,
      // which reads the file natively and only reports a generic network error).
      await file.slice(0, 1).arrayBuffer();
      // audio_only is a yt-dlp download hint: DownloadStep skips the download
      // entirely for an uploaded file, so the flag is meaningless here. Drop it
      // at the boundary rather than clearing the control — the form keeps the
      // user's choice for presets and for switching back to a URL source.
      const fields = {
        language: form.language.value || "",
        audio_only: false,
        transcript: form.transcript.checked,
        prompts: JSON.stringify(getSelectedPrompts()),
        display_name: "",
      };
      const threshold = uploadConfig && Number.isFinite(uploadConfig.chunked_threshold_bytes)
        ? uploadConfig.chunked_threshold_bytes
        : Infinity; // no config -> always single-shot (unchanged behavior)
      if (file.size > threshold) {
        await uploadFileChunked(file, fields);
      } else {
        const fd = new FormData();
        fd.append("file", file);
        if (fields.language) fd.append("language", fields.language);
        fd.append("audio_only", fields.audio_only ? "true" : "false");
        fd.append("transcript", fields.transcript ? "true" : "false");
        fd.append("prompts", fields.prompts);
        await uploadFileWithProgress(fd);
      }
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
  } catch (err) {
    if (isFileReadError(err)) {
      if (fileInput) fileInput.value = "";
      showTaskFormError(t("upload.file_unreadable"));
    } else {
      const message = err && err.message ? err.message : String(err);
      showTaskFormError(t("upload.failed", { message }));
    }
    return;
  }
  form.reset();
  form.transcript.checked = true;
  resetPromptSelection();
  syncSummaryToggle();
  syncSourceType();
  await loadTasks();
}

function syncSummaryToggle() {
  const disabled = !form.transcript.checked;
  // Language only feeds the transcription/summarization steps, which do not run
  // without a transcript. Dim it alongside the prompts so the dependency reads,
  // but never clear the value: currentFormOptions() reads it, so clearing would
  // mark a preset dirty and let a later save overwrite it (see vts-86k).
  const languageControl = document.getElementById("language-control");
  if (languageControl) {
    languageControl.classList.toggle("disabled", disabled);
  }
  form.language.disabled = disabled;
  if (!promptSelect) {
    return;
  }
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
  await apiBatchPost("/api/tasks/" + encodeURIComponent(taskId) + "/restart_summary", { mode });
  await loadTasks();
}

function findTaskEl(taskId) {
  return document.querySelector(`[data-task-id="${taskId}"]`);
}

function patchTaskStatus(taskId, status, errorMessage = "", failureCode = "", queue = undefined) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.baseStatus = String(status || "");
  if (queue !== undefined) {
    runtime.queue = queue || null;
  }
  // specific status, not a group: failure-specific error/code parsing.
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
  // specific status, not a group: running-only timer start (see renderTaskRuntime).
  if (runtime.baseStatus === "running" && !runtime.taskStartedAt) {
    runtime.taskStartedAt = Date.now();
  }
  // specific status, not a group: only a completed run publishes a summary.
  if (runtime.baseStatus === "completed" && runtime.summaryExpected) {
    runtime.summaryReady = true;
    void refreshQueuePositions();
  }
  // specific status, not a group: isFinished() also covers canceled/archived,
  // which would add a final-data fetch this branch never did.
  if (runtime.baseStatus === "completed" || runtime.baseStatus === "failed") {
    void api(`/api/tasks/${taskId}`).then((task) => {
      if (taskEl._runtime === runtime && task) {
        if (task.stats) runtime.stats = parseTaskStats(task);
        runtime.mediaReady = Boolean(task.media_path);
        // Restart capabilities are computed server-side from the task's final
        // steps; SSE patches cannot derive them, so refresh them here or the
        // restart buttons stay disabled until the next loadTasks().
        if (task.capabilities) runtime.capabilities = task.capabilities;
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
        if (task.capabilities) runtime.capabilities = task.capabilities;
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
  // specific status, not a group: only a running task emits download progress.
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
  // Discovered media metadata only fills an empty name — a user rename
  // (e.g. while the task was queued) must survive, same rule as the backend.
  if (!runtime.displayName) {
    runtime.displayName = mediaTitle || mediaFilename;
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
  // specific status, not a group: only a running task emits segment progress.
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
  // specific status, not a group: only `queued` tasks have a queue position to
  // poll; isPending() would also spin the timer up for `waiting` tasks.
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
    patchTaskStatus(payload.task_id, payload.data.status, payload.data.error, payload.data.failure_code, payload.data.queue);
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
  try {
    const cfg = await api("/api/status-config");
    if (cfg && cfg.status_flags) window.statusPred.setFlags(cfg.status_flags);
  } catch { /* predicates degrade to false; loadTasks still renders */ }
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
document.getElementById("file-input")?.addEventListener("change", clearTaskFormError);
form.url.addEventListener("input", clearTaskFormError);
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
      editBtn.className = "icon-btn ghost";
      editBtn.setAttribute("data-tooltip", t("prompts.manage.edit"));
      editBtn.setAttribute("aria-label", t("prompts.manage.edit"));
      editBtn.innerHTML = ICON_EDIT;
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
      delBtn.className = "icon-btn ghost danger";
      delBtn.setAttribute("data-tooltip", t("prompts.manage.delete"));
      delBtn.setAttribute("aria-label", t("prompts.manage.delete"));
      delBtn.innerHTML = ICON_DELETE;
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
    dupBtn.className = "icon-btn ghost";
    dupBtn.setAttribute("data-tooltip", t("prompts.manage.duplicate"));
    dupBtn.setAttribute("aria-label", t("prompts.manage.duplicate"));
    dupBtn.innerHTML = ICON_DUPLICATE;
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

// ---------- Presets manager dialog ----------

const presetsDialog = document.getElementById("presets-dialog");
const presetsListEl = document.getElementById("presets-list");
const presetForm = document.getElementById("preset-form");
const presetEditIdInput = document.getElementById("preset-edit-id");
const presetNameInput = document.getElementById("preset-name-input");
const presetEditLanguage = document.getElementById("preset-edit-language");
const presetEditAudioOnly = document.getElementById("preset-edit-audio_only");
const presetEditTranscript = document.getElementById("preset-edit-transcript");
const presetEditPrompts = document.getElementById("preset-edit-prompts");
const presetSubmitBtn = document.getElementById("preset-submit-btn");
const presetCancelBtn = document.getElementById("preset-cancel-btn");

let presetsManagerDefaultRef = null;

function presetRefEquals(a, b) {
  return !!a && !!b && a.source === b.source && String(a.id) === String(b.id);
}

function setPresetFormMode(editId) {
  if (presetEditIdInput) presetEditIdInput.value = editId || "";
  if (presetSubmitBtn) {
    presetSubmitBtn.textContent = editId
      ? t("preset.manage.edit")
      : t("preset.manage.create");
  }
  if (presetCancelBtn) presetCancelBtn.classList.toggle("hidden", !editId);
}

function resetPresetForm() {
  if (presetNameInput) presetNameInput.value = "";
  if (presetEditLanguage) presetEditLanguage.value = "";
  if (presetEditAudioOnly) presetEditAudioOnly.checked = false;
  if (presetEditTranscript) presetEditTranscript.checked = true;
  if (presetEditPrompts) {
    renderPromptMultiselect(
      presetEditPrompts,
      promptsCache,
      [{ source: "system", id: "summary" }],
      { flat: true },
    );
  }
  setPresetFormMode("");
}

function fillPresetForm(preset) {
  if (!presetForm) return;
  if (presetNameInput) presetNameInput.value = preset.name || "";
  const opts = preset.options || {};
  if (presetEditLanguage) presetEditLanguage.value = opts.language || "";
  if (presetEditAudioOnly) presetEditAudioOnly.checked = !!opts.audio_only;
  if (presetEditTranscript) presetEditTranscript.checked = !!opts.transcript;
  if (presetEditPrompts) {
    const { filtered } = filterDanglingPrompts(opts.prompts);
    renderPromptMultiselect(presetEditPrompts, promptsCache, filtered, {
      flat: true,
    });
  }
  setPresetFormMode(preset.id);
  presetNameInput?.focus();
}

async function duplicatePreset(preset) {
  try {
    await api("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `${presetLabel(preset)}${t("preset.copy_suffix")}`,
        options: preset.options || {},
      }),
    });
  } catch (err) {
    console.error("Failed to duplicate preset", err);
    return;
  }
  await refreshPresetsManager();
  await loadPresets();
}

async function makePresetDefault(preset) {
  try {
    await api("/api/me/default_preset", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: preset.source, id: preset.id }),
    });
  } catch (err) {
    console.error("Failed to set default preset", err);
    return;
  }
  await refreshPresetsManager();
  await loadPresets();
}

async function deletePreset(preset) {
  if (!window.confirm(`${t("preset.manage.delete")}: ${preset.name}?`)) {
    return;
  }
  try {
    const resp = await fetch(buildPath(`/api/presets/${encodeURIComponent(preset.id)}`), {
      method: "DELETE",
    });
    if (!resp.ok) return;
  } catch (err) {
    console.error("Failed to delete preset", err);
    return;
  }
  if (presetEditIdInput?.value === String(preset.id)) resetPresetForm();
  await refreshPresetsManager();
  await loadPresets();
}

function renderPresetsList(presets, defaultRef) {
  if (!presetsListEl) return;
  presetsListEl.innerHTML = "";
  for (const preset of presets) {
    const row = document.createElement("div");
    row.className = "tokens-row prompts-row";

    const meta = document.createElement("div");
    meta.className = "tokens-meta prompts-meta";
    const name = document.createElement("span");
    name.className = "tokens-name prompt-name";
    name.textContent = presetLabel(preset);
    meta.appendChild(name);
    if (preset.source === "system") {
      const badge = document.createElement("span");
      badge.className = "prompt-badge prompt-badge-system";
      badge.textContent = t("preset.manage.system_badge");
      meta.appendChild(badge);
    }
    if (presetRefEquals({ source: preset.source, id: preset.id }, defaultRef)) {
      const badge = document.createElement("span");
      badge.className = "prompt-badge prompt-badge-default";
      badge.textContent = t("preset.manage.default_badge");
      meta.appendChild(badge);
    }
    row.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "prompts-actions";

    if (preset.source === "user" && preset.editable) {
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "icon-btn ghost";
      editBtn.setAttribute("data-tooltip", t("preset.manage.edit"));
      editBtn.setAttribute("aria-label", t("preset.manage.edit"));
      editBtn.innerHTML = ICON_EDIT;
      editBtn.addEventListener("click", () => fillPresetForm(preset));
      actions.appendChild(editBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "icon-btn ghost danger";
      delBtn.setAttribute("data-tooltip", t("preset.manage.delete"));
      delBtn.setAttribute("aria-label", t("preset.manage.delete"));
      delBtn.innerHTML = ICON_DELETE;
      delBtn.addEventListener("click", () => deletePreset(preset));
      actions.appendChild(delBtn);
    }

    const dupBtn = document.createElement("button");
    dupBtn.type = "button";
    dupBtn.className = "icon-btn ghost";
    dupBtn.setAttribute("data-tooltip", t("preset.manage.duplicate"));
    dupBtn.setAttribute("aria-label", t("preset.manage.duplicate"));
    dupBtn.innerHTML = ICON_DUPLICATE;
    dupBtn.addEventListener("click", () => duplicatePreset(preset));
    actions.appendChild(dupBtn);

    const defBtn = document.createElement("button");
    defBtn.type = "button";
    defBtn.className = "icon-btn ghost";
    defBtn.setAttribute("data-tooltip", t("preset.manage.make_default"));
    defBtn.setAttribute("aria-label", t("preset.manage.make_default"));
    defBtn.innerHTML = ICON_MAKE_DEFAULT;
    defBtn.addEventListener("click", () => makePresetDefault(preset));
    actions.appendChild(defBtn);

    row.appendChild(actions);
    presetsListEl.appendChild(row);
  }
}

async function refreshPresetsManager() {
  if (!presetsListEl) return;
  try {
    const presets = await api("/api/presets");
    let defaultRef = null;
    try {
      defaultRef = await api("/api/me/default_preset");
    } catch (err) {
      console.error("Failed to load default preset", err);
    }
    presetsManagerDefaultRef = defaultRef;
    renderPresetsList(Array.isArray(presets) ? presets : [], defaultRef);
  } catch (err) {
    console.error("Failed to load presets", err);
  }
}

document.getElementById("presets-btn")?.addEventListener("click", async () => {
  if (!presetsDialog) return;
  resetPresetForm();
  await loadPrompts();
  await refreshPresetsManager();
  if (typeof presetsDialog.showModal === "function") {
    presetsDialog.showModal();
  } else {
    presetsDialog.setAttribute("open", "");
  }
});

document.getElementById("presets-close-btn")?.addEventListener("click", () => {
  presetsDialog?.close();
});

document.getElementById("task-about-close-btn")?.addEventListener("click", () => {
  taskAboutDialog?.close();
});

presetCancelBtn?.addEventListener("click", () => {
  resetPresetForm();
});

presetForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const editId = presetEditIdInput?.value || "";
  const name = (presetNameInput?.value || "").trim();
  if (!name) return;
  const options = {
    language: presetEditLanguage ? presetEditLanguage.value || "" : "",
    audio_only: !!(presetEditAudioOnly && presetEditAudioOnly.checked),
    transcript: !!(presetEditTranscript && presetEditTranscript.checked),
    prompts: getSelectedFrom(presetEditPrompts),
  };
  try {
    let resp;
    if (editId) {
      resp = await fetch(buildPath(`/api/presets/${encodeURIComponent(editId)}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, options }),
      });
    } else {
      resp = await fetch(buildPath("/api/presets"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, options }),
      });
    }
    if (!resp.ok) return;
  } catch (err) {
    console.error("Failed to save preset", err);
    return;
  }
  resetPresetForm();
  await refreshPresetsManager();
  await loadPresets();
});

// ---------- Restart final dialog ----------

const restartFinalDialog = document.getElementById("restart-final-dialog");
const restartFinalSelect = document.getElementById("restart-final-select");
const restartFinalCloseBtn = document.getElementById("restart-final-close-btn");
const restartFinalSubmitBtn = document.getElementById("restart-final-submit-btn");
const restartFinalPreset = document.getElementById("restart-final-preset");
let restartFinalTaskId = null;

function updateRestartFinalSubmitState() {
  if (!restartFinalSubmitBtn) return;
  restartFinalSubmitBtn.disabled = getSelectedFrom(restartFinalSelect).length === 0;
}

async function populateRestartFinalPresets() {
  if (!restartFinalPreset) {
    return;
  }
  if (!presetsCache.length) {
    await loadPresets();
  }
  restartFinalPreset.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = t("restart_final.preset_none");
  restartFinalPreset.appendChild(none);
  for (const preset of presetsCache) {
    const opt = document.createElement("option");
    opt.value = `${preset.source}:${preset.id}`;
    opt.textContent = presetLabel(preset);
    restartFinalPreset.appendChild(opt);
  }
  restartFinalPreset.value = ""; // reset to the neutral item on each open
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
  renderPromptMultiselect(restartFinalSelect, prompts, selected, { flat: true });
  await populateRestartFinalPresets();
  updateRestartFinalSubmitState();
  if (typeof restartFinalDialog.showModal === "function") {
    restartFinalDialog.showModal();
  } else {
    restartFinalDialog.setAttribute("open", "");
  }
}

restartFinalSelect?.addEventListener("change", updateRestartFinalSubmitState);

restartFinalPreset?.addEventListener("change", () => {
  const value = restartFinalPreset.value;
  if (!value) {
    return; // "—" selected: leave the current multiselect as-is
  }
  const idx = value.indexOf(":");
  const source = value.slice(0, idx);
  const id = value.slice(idx + 1);
  const preset = presetsCache.find((p) => p.source === source && p.id === id);
  if (!preset) {
    return;
  }
  const promptRefs = (preset.options && preset.options.prompts) || [];
  const { filtered } = filterDanglingPrompts(promptRefs);
  renderPromptMultiselect(restartFinalSelect, promptsCache, filtered, { flat: true });
  updateRestartFinalSubmitState();
});

restartFinalCloseBtn?.addEventListener("click", () => {
  restartFinalDialog?.close();
});

restartFinalSubmitBtn?.addEventListener("click", async () => {
  const prompts = getSelectedFrom(restartFinalSelect);
  if (!prompts.length || restartFinalTaskId == null) return;
  await apiBatchPost("/api/tasks/" + encodeURIComponent(restartFinalTaskId) + "/restart_summary", {
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

async function loadProgressWeights() {
  try {
    const data = await api("/api/progress-weights");
    if (data && data.weights && typeof data.weights === "object") {
      serverStepWeights = data.weights;
      serverFinalFallback = Number.isFinite(Number(data.final_summary_fallback))
        ? Number(data.final_summary_fallback)
        : null;
    }
  } catch {
    // keep nulls -> getStepWeight falls back to hardcoded STEP_WEIGHT_SECONDS
  }
}

async function loadUploadConfig() {
  try {
    uploadConfig = await api("/api/uploads/config");
  } catch {
    uploadConfig = null; // fall back to single-shot for all sizes
  }
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
  // Load prompts before the first task render so per-prompt finalize step
  // labels resolve to names (not the raw "finalize:user:<uuid>") on first paint.
  await loadPrompts();
  await refreshAll();
  await loadPresets();
  await loadPushConfig();
  await loadProgressWeights();
  await loadUploadConfig();
}

void bootstrap();

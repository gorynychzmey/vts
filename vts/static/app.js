const ICON_EDIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.58z"/></svg>';
const ICON_DELETE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';
const ICON_DUPLICATE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
// Two arrows converging into one — "merge these people into one person".
const ICON_MERGE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 3v4a5 5 0 0 0 5 5h6M6 21v-4a5 5 0 0 1 5-5h6"/><path d="M14 9l3 3-3 3"/></svg>';
// Arrow leaving a box — "move this fragment somewhere else".
const ICON_MOVE = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/><path d="M14 4h6v6"/><path d="M20 4l-8 8"/></svg>';
const ICON_MAKE_DEFAULT ='<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l3 6.5 7 .9-5 4.8 1.3 7L12 17.8 5.4 21.2 6.7 14.2 1.7 9.4l7-.9z"/></svg>';
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

// Task-list filter state (vts-rhx). Hoisted next to the other early module
// state because loadFirstPage() reads it during bootstrap; a `const` declared
// down with the filter code would be in its temporal dead zone by then.
const FILTERS_STORAGE_KEY = "vts.taskFilters";

const filterInputs = {
  q: document.getElementById("filter-q"),
  type: document.getElementById("filter-type"),
  from: document.getElementById("filter-from"),
  to: document.getElementById("filter-to"),
};

// Delivery state (vts-j2kh). Declared up here, not next to the delivery code
// far below: bootstrap's loadPresets() -> applyPresetOptions() reads it, and a
// `const` declared later would be in its temporal dead zone at that point.
const deliveryState = {
  adapters: [],
  incompatible: {},
  credentials: [],
  targets: [],
  variants: [],
};
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
  // An explicit choice outranks navigator.languages; absence means "detect".
  let stored = null;
  try {
    stored = localStorage.getItem("vts_locale");
  } catch (err) {
    stored = null;
  }
  const preferred = SUPPORTED_LOCALES.has(stored) ? stored : detectLocale();
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
  queueRefreshInFlight: false,
  taskPaging: {
    head: null, tail: null, pageSize: 10,
    loading: false, exhausted: false, newIds: new Set(), epoch: 0,
    // Tasks this tab just created. The server publishes task_status BEFORE
    // returning the HTTP response, so for a slow create (a chunked upload)
    // the SSE event arrives while the request is still in flight — with no
    // card in the DOM yet, it was flagged as "new tasks (1)" instead of
    // appearing at the top of the list (vts-3iw).
    ownIds: new Set(),
  }
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

// Only http(s) URLs are safe to assign as an anchor href. Guards against a
// task whose source_url is e.g. "javascript:..." producing a clickable title
// that runs script in the user's own session (vts-dcc self-XSS).
function isHttpUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value);
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
  // Read the strip rather than a hardcoded list: the prompt tabs are inserted
  // between "summary" and "log", and a task whose only ready result is a user
  // prompt must be able to fall back onto it.
  const buttons = taskEl ? [...taskEl.querySelectorAll(".tab-btn")] : [];
  for (const btn of buttons) {
    const tabName = String(btn.dataset.tab || "");
    if (tabName && !btn.disabled) {
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
  // Prompt results are markdown like the summary; the tab name already carries
  // the prompt identity, so it doubles as the filename stem.
  if (isPromptTabName(tabName)) {
    return { prefix: tabName, ext: "md" };
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
    // The summary tab renders system:summary specifically. It used to be the
    // generic "results" view fronted by a dropdown; user prompts now have their
    // own tabs, so this is just one more result.
    return loadPromptResult(taskEl, taskId, { source: "system", id: "summary" }, "summary");
  }
  if (isPromptTabName(tabName)) {
    const ref = promptTabRef(taskEl, tabName);
    if (!ref) {
      return "";
    }
    return loadPromptResult(taskEl, taskId, ref, tabName);
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

// Flag which sides of the tab strip have content scrolled out of view, so CSS
// can fade that edge. Without it a strip that overflows just looks truncated —
// nothing indicates it can be scrolled (Diana, on mobile; it applies to a
// desktop card with many prompt tabs too).
function updateTabsScrollHints(tabsBar) {
  if (!tabsBar) {
    return;
  }
  // 1px of slack: scrollWidth/clientWidth are integers but the used widths are
  // fractional, so an unscrollable strip can report a 0.5px difference.
  const max = tabsBar.scrollWidth - tabsBar.clientWidth;
  const left = tabsBar.scrollLeft;
  tabsBar.dataset.scrollStart = left > 1 ? "1" : "0";
  tabsBar.dataset.scrollEnd = max - left > 1 ? "1" : "0";
}

// Bound once per card. ResizeObserver covers the cases a scroll listener misses:
// the card being expanded, the window resizing, and a prompt tab being added.
function bindTabsScrollHints(tabsBar) {
  if (!tabsBar || tabsBar._scrollHintsBound) {
    return;
  }
  tabsBar._scrollHintsBound = true;
  tabsBar.addEventListener("scroll", () => updateTabsScrollHints(tabsBar), { passive: true });
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => updateTabsScrollHints(tabsBar));
    ro.observe(tabsBar);
  }
  updateTabsScrollHints(tabsBar);
}

// A prompt ref -> the tab name used in `data-tab`, the panel's CSS class and
// the download filename. Tabs are addressed as `.tab-content.<name>` and
// `[data-tab="<name>"]`, so the name has to survive both a CSS class selector
// and an attribute selector: user prompt ids are UUIDs and system ids are
// slugs, but neither is guaranteed class-safe, so anything outside [a-z0-9_-]
// is escaped rather than trusted.
function promptTabName(ref) {
  const source = String(ref && ref.source ? ref.source : "");
  const id = String(ref && ref.id ? ref.id : "");
  const safe = `${source}-${id}`.replace(/[^a-zA-Z0-9_-]/g, "_");
  return `prompt_${safe}`;
}

function isPromptTabName(tabName) {
  return String(tabName || "").startsWith("prompt_");
}

// The {source,id} behind a prompt tab, read back off the button rather than
// re-derived from the name — promptTabName is lossy (it escapes), so it cannot
// be inverted.
function promptTabRef(taskEl, tabName) {
  const btn = getTabButton(taskEl, tabName);
  if (!btn) {
    return null;
  }
  const source = String(btn.dataset.promptSource || "");
  const id = String(btn.dataset.promptId || "");
  return source && id ? { source, id } : null;
}

// Status of a prompt ref within runtime.promptResults ("" when the pipeline has
// not reported on it yet).
function promptResultStatus(taskEl, ref) {
  const entries = taskEl && taskEl._runtime && Array.isArray(taskEl._runtime.promptResults)
    ? taskEl._runtime.promptResults
    : [];
  const hit = entries.find(
    (e) => e && String(e.source) === ref.source && String(e.id) === ref.id,
  );
  return hit ? String(hit.status || "") : "";
}

// Build/refresh one tab per selected USER prompt, keeping "Log" last.
//
// system:summary is deliberately excluded: it already has its own "Summary"
// tab, and it appears in prompt_results alongside the user prompts, so
// including it here would render the same result twice.
//
// Tabs follow runtime.promptRefs (selection order, stable from creation) and
// take only their enabled/disabled state from prompt_results, so a tab does not
// jump position when its prompt finishes. Existing buttons are reused rather
// than rebuilt: recreating them on every poll would drop the click handler
// bound here and reset the strip's scroll position mid-read.
function syncPromptTabs(taskEl, taskId) {
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const tabsBar = taskEl.querySelector(".tabs");
  if (!tabsBar) {
    return;
  }
  const refs = (Array.isArray(taskEl._runtime.promptRefs) ? taskEl._runtime.promptRefs : [])
    .filter((ref) => ref && ref.source === "user");
  const logBtn = getTabButton(taskEl, "log");
  const wanted = new Set(refs.map((ref) => promptTabName(ref)));

  // Drop tabs whose prompt is no longer selected (restart with a new preset).
  for (const btn of [...tabsBar.querySelectorAll(".tab-btn")]) {
    const name = String(btn.dataset.tab || "");
    if (isPromptTabName(name) && !wanted.has(name)) {
      btn.remove();
      getTabPanel(taskEl, name)?.remove();
    }
  }

  for (const ref of refs) {
    const tabName = promptTabName(ref);
    let btn = getTabButton(taskEl, tabName);
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tab-btn";
      btn.dataset.tab = tabName;
      btn.dataset.promptSource = ref.source;
      btn.dataset.promptId = ref.id;
      // NO data-i18n here: applyI18n assigns textContent, and these labels are
      // user-authored prompt names that carry no translation key anyway.
      btn.addEventListener("click", async () => {
        if (btn.disabled) {
          return;
        }
        await activateTaskTab(taskEl, taskId, tabName);
      });
      // Keep "Log" last (Victor): insert before it rather than appending.
      tabsBar.insertBefore(btn, logBtn || null);
    }
    let panel = getTabPanel(taskEl, tabName);
    if (!panel) {
      panel = document.createElement("pre");
      panel.className = `tab-content ${tabName}`;
      const logPanel = getTabPanel(taskEl, "log");
      logPanel?.parentNode?.insertBefore(panel, logPanel);
    }
    const label = promptDisplayName({
      source: ref.source,
      id: ref.id,
      name: aboutResolvePromptName(ref.source, ref.id),
    });
    if (btn.textContent !== label) {
      btn.textContent = label;
    }
    const ready = promptResultStatus(taskEl, ref) === "completed";
    btn.disabled = !ready;
    // Same "explain why it is dead" treatment the built-in tabs get.
    btn.title = ready ? label : `${label}${t("results.pending")}`;
    btn.setAttribute("aria-label", btn.title);
  }

  bindTabsScrollHints(tabsBar);
  updateTabsScrollHints(tabsBar);
}

// Load one prompt result into its tab panel.
//
// The legacy /summary fallback is kept for system:summary specifically: a
// summary-only task can finish with summary_path set before prompt_results is
// populated, and the per-result endpoint 404s for an entry that is not there
// yet (vts-b6l). User prompts have no such legacy path — an absent result just
// means the tab is still disabled.
async function loadPromptResult(taskEl, taskId, ref, tabName) {
  const panel = getTabPanel(taskEl, tabName);
  const source = String(ref && ref.source ? ref.source : "");
  const id = String(ref && ref.id ? ref.id : "");
  const known = promptResultStatus(taskEl, { source, id }) !== "";
  let text;
  if (source === "system" && id === "summary" && !known) {
    text = await api(`/api/tasks/${taskId}/summary`).catch((err) => err.message);
  } else {
    text = await api(
      `/api/tasks/${taskId}/results/${encodeURIComponent(source)}/${encodeURIComponent(id)}`
    ).catch((err) => err.message);
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
  if (tab === "transcript" || tab === "summary" || tab === "redacted" || isPromptTabName(tab)) {
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

// Sum of the already-finished steps' own durations, in ms. Mirrors the
// backend's _processing_seconds_for_task: work time is the sum of per-step
// durations, NOT the span from the first start to the last finish — a task can
// sit paused / awaiting input for hours between steps, and that idle gap is not
// work. Only finished steps count here; the still-running step's elapsed time
// is added live by the timer tick (see updateTaskRuntimeView).
function computeCompletedStepMs(task) {
  let total = 0;
  for (const step of task.steps || []) {
    const started = parseIsoMs(step.started_at);
    const finished = parseIsoMs(step.finished_at);
    if (started === null || finished === null) {
      continue;
    }
    const stepMs = finished - started;
    if (stepMs > 0) {
      total += stepMs;
    }
  }
  return total;
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
    mediaBytes: parseNonNegativeInt(stats && stats.media_bytes),
    // How many files the user actually uploaded. Not from `stats` (which
    // describes the single concatenated media file) but from the options the
    // finalize step already writes for every multi-file upload — so this works
    // retroactively on tasks created before this line existed, with no
    // migration. Single-file and link tasks have no source_files at all.
    sourceFileCount: Array.isArray(task && task.options && task.options.source_files)
      ? task.options.source_files.length
      : 0
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
  // Only for a genuine multi-file task (Diana): "1 файл" next to every single
  // upload would be noise, and the count is what disambiguates a task whose
  // title shows just the first file's name.
  if (stats.sourceFileCount > 1) {
    parts.push(t("stats.media_files", { count: stats.sourceFileCount }));
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

  // Title mirrors the card's .task-link behavior: the local player when media
  // is available (uploads AND link tasks alike), unlinked otherwise. The
  // original URL, when there is one, is the separate .about-source-url link.
  const sourceUrl = task.source_url || "";
  const isUpload = sourceUrl.startsWith("file://");
  const uploadName = isUpload ? sourceUrl.slice("file://".length) : "";
  const titleEl = q(".about-source-title");
  titleEl.textContent = task.source_title || (isUpload ? uploadName : sourceUrl);
  const mediaReady = Boolean(task.media_path);
  const playerHref = buildPath(`/player/${encodeURIComponent(task.id)}`);
  if (mediaReady) {
    titleEl.href = playerHref;
    titleEl.target = "_blank";
    titleEl.rel = "noopener";
  } else {
    titleEl.removeAttribute("href");
  }
  // Player ▶ icon next to the title, mirroring the task card (vts-u6w #1).
  const playerBtn = q(".about-player-btn");
  if (playerBtn) {
    if (mediaReady) {
      playerBtn.href = playerHref;
      playerBtn.classList.remove("hidden");
    } else {
      playerBtn.removeAttribute("href");
      playerBtn.classList.add("hidden");
    }
  }
  const sourceUrlEl = q(".about-source-url");
  // Original url: plain text for uploads (file://…), a real link for http(s)
  // sources. isHttpUrl guards against javascript:/data: hrefs (vts-dcc).
  sourceUrlEl.textContent = isUpload ? uploadName : sourceUrl;
  if (isHttpUrl(sourceUrl)) {
    sourceUrlEl.href = sourceUrl;
    sourceUrlEl.target = "_blank";
    sourceUrlEl.rel = "noopener noreferrer";
  } else {
    sourceUrlEl.removeAttribute("href");
  }
  // A set was joined into one recording; list the parts and say which rule
  // decided their order, since the user cannot change it (vts-vm0).
  const sourceFiles = Array.isArray(options.source_files) ? options.source_files : [];
  const filesEl = q(".about-source-files");
  if (filesEl) {
    if (sourceFiles.length > 1) {
      // Assumes the closed enum resolve_order() returns (vts/services/upload_order.py):
      // "creation_time" | "last_modified" | "filename", with an unconditional
      // filename fallback. Adding a fourth order value means adding its
      // about.order_* key to all three locale files too, or it renders raw.
      const orderKey = `about.order_${options.source_files_order || "filename"}`;
      const lines = sourceFiles.map((f, i) => `${i + 1}. ${f.name}`);
      filesEl.textContent = `${t("about.source_files")} (${t(orderKey)}): ${lines.join("; ")}`;
      filesEl.classList.remove("hidden");
    } else {
      filesEl.textContent = "";
      filesEl.classList.add("hidden");
    }
  }
  q(".about-created").textContent = task.created_at
    ? new Date(task.created_at).toLocaleString()
    : "";

  q(".about-language").textContent = options.language || t("about.language_auto");
  setAboutBool(q(".about-audio-only"), Boolean(options.audio_only));
  setAboutBool(q(".about-transcript"), options.transcript !== false);
  // Unlike transcript (default on), diarize defaults off — a task predating the
  // flag, or one whose options never carried it, did not diarize.
  setAboutBool(q(".about-diarize"), Boolean(options.diarize));
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
    awaitingStep: typeof task.awaiting_step === "string" ? task.awaiting_step : "",
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
    // Selected prompts in the order the user picked them, independent of
    // prompt_results (which is APPEND-ORDERED BY COMPLETION — driving tabs off
    // it would make them appear one by one and reorder as each finishes).
    // Diana's ask is that the tabs are visible from the moment the task is
    // created, so the tab strip follows this list and prompt_results only
    // supplies each tab's status.
    promptRefs: selectedPromptRefs((task.options || {})),
    redactedReady: Boolean(task.redacted_path),
    mediaReady: Boolean(task.media_path),
    currentStepName: runningStep ? runningStep.name : failedStep ? failedStep.name : "",
    failedStepName: failedStep ? failedStep.name : "",
    currentStepStartedAt: runningStep ? parseIsoMs(runningStep.started_at) : null,
    taskStartedAt: computeTaskStartedAt(task),
    completedStepMs: computeCompletedStepMs(task),
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
    // Only the `embeddings` step of diarization reports a total (it is ~98% of
    // the wall time); the others fire once with total 0 and read as running.
    diarize: {
      step: "",
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
  // awaiting_input is a real STOP with every step that has run already finished,
  // and no running step to point at. Like completed, it must NOT fall through to
  // the download heuristic below (a leftover SSE download flag would resolve the
  // label back to "download"/"extract_audio" — an early step — vts-h3u). Unlike
  // completed it has NOT run its whole enabled list (it paused at match_speakers,
  // which isn't even an enabled step), so resolve to the LAST FINISHED enabled
  // step rather than the last enabled one, which could be a not-yet-run finalize.
  if (statusPred.needsInput(runtime.baseStatus)) {
    for (let i = runtime.enabledSteps.length - 1; i >= 0; i--) {
      if (isStepFinishedStatus(runtime.stepStatusByName[runtime.enabledSteps[i]] || "")) {
        return runtime.enabledSteps[i];
      }
    }
    return "";
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
  } else if (active === "diarize") {
    // A percentage only during the embeddings pass, which dominates the step.
    // The brief segmentation/counting phases carry no total, so they show as
    // running rather than snapping the bar back to 0%.
    if (runtime.diarize.step === "embeddings" && runtime.diarize.total > 0) {
      const current = Math.max(0, Math.min(runtime.diarize.current, runtime.diarize.total));
      value = normalizeProgress(current / runtime.diarize.total);
      textOverride = `${current}/${runtime.diarize.total}`;
    } else {
      indeterminate = true;
    }
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

  // awaiting_input is a real STOP, not an active step: shows_progress is false
  // server-side. Rendering the indeterminate "working" runner (the !active
  // fallback below) made a paused-for-review task look like it was still
  // processing (vts-552). Show a steady, non-animated bar with the status label
  // instead — the work that ran is done; it is now waiting on the human.
  if (statusPred.needsInput(runtime.baseStatus)) {
    return { value: 0, indeterminate: false, text: t("status.awaiting_input") };
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
  // Our local player works for uploads AND downloaded-by-link tasks alike:
  // _find_media_file just checks for the media file on disk. When the media is
  // gone (TTL / archive / not-yet-downloaded), there's nothing to play — the
  // name goes unclickable + "expired", matching what uploads already did.
  const mediaReady = Boolean(runtime.mediaReady);
  const playerHref = buildPath(`/player/${encodeURIComponent(runtime.id)}`);

  const linkLabel = hasName ? runtime.displayName : (isUpload ? uploadName : runtime.sourceUrl);
  // Into the inner span, NOT the anchor: the anchor also holds the player glyph,
  // and assigning textContent to it would delete that glyph on every render.
  const linkTextEl = elements.linkEl.querySelector(".task-link-text");
  if (linkTextEl) linkTextEl.textContent = linkLabel;
  else elements.linkEl.textContent = linkLabel;
  if (mediaReady) {
    elements.linkEl.href = playerHref;
    elements.linkEl.target = "_blank";
    elements.linkEl.rel = "noopener";
    elements.linkEl.classList.remove("expired");
  } else {
    elements.linkEl.removeAttribute("href");
    elements.linkEl.removeAttribute("target");
    elements.linkEl.removeAttribute("rel");
    elements.linkEl.classList.add("expired");
  }

  // The player lives in the task menu (redesign v2). It used to be an icon next
  // to the title for discoverability (vts-at8); the design moved it into the
  // menu, where it gets a readable label instead of a bare glyph — the same
  // trade the other five actions made.
  if (elements.playerBtn) {
    if (mediaReady) {
      elements.playerBtn.href = playerHref;
      elements.playerBtn.classList.remove("hidden");
    } else {
      elements.playerBtn.removeAttribute("href");
      elements.playerBtn.classList.add("hidden");
    }
  }

  // The glyph inside the title marks the name as playable — shown on exactly the
  // same condition that makes the name a link at all.
  elements.linkEl.querySelector(".player-glyph")?.classList.toggle("hidden", !mediaReady);

  if (elements.expiredEl) {
    elements.expiredEl.classList.toggle("hidden", mediaReady);
  }

  // The source line under the title: for link tasks, the ORIGINAL url as a
  // real clickable link (shown always, so the original is always reachable
  // even when the name now points at our player). For uploads there is no
  // original url — show the filename as plain text, and only when a custom
  // display name is set (otherwise the name IS the filename).
  const sourceEl = elements.sourceEl;
  if (isUpload) {
    sourceEl.textContent = uploadName;
    sourceEl.removeAttribute("href");
    sourceEl.classList.toggle("hidden", !hasName);
  } else {
    sourceEl.textContent = runtime.sourceUrl;
    if (isHttpUrl(runtime.sourceUrl)) {
      sourceEl.href = runtime.sourceUrl;
      sourceEl.target = "_blank";
      sourceEl.rel = "noopener noreferrer";
    } else {
      sourceEl.removeAttribute("href");
    }
    sourceEl.classList.remove("hidden");
  }
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
  // The status also drives the card itself (redesign v2): a coloured left edge
  // and the dot in the header row. Carried as a class on the card so the CSS
  // owns the mapping — the pill's own class is scoped to the pill.
  for (const cls of Array.from(taskEl.classList)) {
    if (cls.startsWith("status-")) taskEl.classList.remove(cls);
  }
  taskEl.classList.add(`status-${runtime.baseStatus}`);
  const canPause = statusPred.canPause(runtime.baseStatus);
  const canResume = statusPred.canResume(runtime.baseStatus);
  const canRestartSummary = statusPred.canRestartSummary(runtime);
  const canRestartFinalSummary = statusPred.canRestartFinalSummary(runtime);
  const canArchive = statusPred.canArchive(runtime.baseStatus);
  elements.pauseBtn.disabled = !canPause;
  elements.resumeBtn.disabled = !canResume;
  if (elements.resolveVoicesBtn) {
    // Show when the task is paused at match_speakers awaiting input, OR when the
    // backend reports the task can (re)resolve speakers regardless of status —
    // e.g. editing bindings on a completed task (vts-552). Only one awaiting_step
    // dispatches today (match_speakers); a future step would need its own dialog.
    const paused = statusPred.needsInput(runtime.baseStatus) && runtime.awaitingStep === "match_speakers";
    const canResolve = Boolean(runtime.capabilities && runtime.capabilities.can_resolve_speakers);
    const showResolve = paused || canResolve;
    elements.resolveVoicesBtn.classList.toggle("hidden", !showResolve);
    elements.resolveVoicesBtn.disabled = !showResolve;
  }
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
  // BEFORE ensureActiveTabSelection: that call picks a fallback tab when the
  // active one is disabled, so the prompt tabs have to exist and carry their
  // enabled state by then, or a task whose only ready result is a user prompt
  // would fall back past it.
  syncPromptTabs(taskEl, taskEl.dataset.taskId || "");
  ensureActiveTabSelection(taskEl);

  // specific status, not a group: only a running task's elapsed timer ticks;
  // a waiting task is not executing, so it must keep a blank runtime.
  //
  // Work time = sum of finished steps' durations + the current step's live
  // elapsed. This deliberately excludes idle gaps between steps (pause /
  // awaiting input), so it matches the backend's processing_seconds and never
  // counts pause time as work. (The old code showed now - firstStepStart,
  // which ballooned across long pauses.)
  if (runtime.baseStatus === "running") {
    let elapsedMs = runtime.completedStepMs || 0;
    if (runtime.currentStepStartedAt) {
      const currentStepMs = Date.now() - runtime.currentStepStartedAt;
      if (currentStepMs > 0) {
        elapsedMs += currentStepMs;
      }
    }
    elements.taskRuntimeEl.textContent = formatDuration(elapsedMs / 1000);
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

function renderTaskCard(task) {
  const node = taskTemplate.content.cloneNode(true);
  const root = node.querySelector(".task");
  const body = node.querySelector(".task-body");
  const toggleBtn = root.querySelector(".toggle-btn");
  const taskRightTop = root.querySelector(".task-right-top");
  const toolbarWrap = root.querySelector(".task-toolbar-wrap");
  const toolbarScroll = root.querySelector(".task-right-bottom");
  const pauseBtn = root.querySelector(".pause-btn");
  const resumeBtn = root.querySelector(".resume-btn");
  const resolveVoicesBtn = root.querySelector(".resolve-voices-btn");
  const speakerRegistryLink = root.querySelector(".speaker-box-registry-btn");
  const taskMenuBtn = root.querySelector(".task-menu-btn");
  const taskMenu = root.querySelector(".task-menu");
  const taskAboutBtn = root.querySelector(".task-about-btn");
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
  root.dataset.createdAt = task.created_at;
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
  if (resolveVoicesBtn) {
    resolveVoicesBtn.setAttribute("data-tooltip", t("action.resolve_voices"));
    resolveVoicesBtn.setAttribute("aria-label", t("action.resolve_voices"));
  }
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
    // Prompt tabs are labelled with the user's own prompt name by
    // syncPromptTabs and have no `tab.*` key; the fallback here is the raw tab
    // name, so relabelling one would replace "Протокол" with "prompt_user-<id>".
    if (isPromptTabName(tabName)) {
      return;
    }
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
      // First expand only: /speaker-matches is a real request and most cards are
      // never opened.
      if (!root._speakerRows) void loadSpeakerPanel(root, task.id);
      else renderSpeakerPanel(root, task.id);
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
  if (resolveVoicesBtn) {
    resolveVoicesBtn.addEventListener("click", () => {
      // Read live runtime at click time: paused === awaiting_input drives
      // "Save & continue" visibility. (Per-speaker duration now comes from
      // each row's own diarized seconds, not media length — vts-552.)
      const rt = root._runtime;
      const paused = Boolean(rt && rt.baseStatus === "awaiting_input");
      openVoiceDialog(task.id, paused);
    });
  }
  if (taskMenuBtn && taskMenu) {
    taskMenuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = taskMenu.classList.contains("open");
      document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
      if (!isOpen) {
        // Measure the trigger, then right-align the panel to it — the same
        // placement the restart menu uses, so a wide panel cannot hang off the
        // screen edge on a phone.
        const rect = taskMenuBtn.getBoundingClientRect();
        taskMenu.style.top = `${rect.bottom + 4}px`;
        taskMenu.style.left = "0px";
        taskMenu.classList.add("open");
        taskMenu.style.left = `${Math.max(8, rect.right - taskMenu.offsetWidth)}px`;
      }
      taskMenuBtn.setAttribute("aria-expanded", String(!isOpen));
      // The card must paint above its neighbours while its menu is open.
      root.classList.toggle("menu-open", !isOpen);
    });
    // Every entry either opens a dialog or acts immediately, so the menu closes
    // on any click inside it — except Restart, which swaps in its own panel.
    taskMenu.addEventListener("click", (e) => {
      if (e.target instanceof Element && e.target.closest(".restart-summary-btn")) return;
      taskMenu.classList.remove("open");
      taskMenuBtn.setAttribute("aria-expanded", "false");
      root.classList.remove("menu-open");
    });
  }
  if (speakerRegistryLink) {
    speakerRegistryLink.addEventListener("click", () => {
      document.getElementById("speaker-registry-btn")?.click();
    });
  }
  if (taskAboutBtn) {
    taskAboutBtn.addEventListener("click", () => openTaskAboutDialog(task));
  }
  if (restartSummaryBtn && restartSummaryMenu) {
    restartSummaryBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = restartSummaryMenu.classList.contains("open");
      // Closes the kebab too: the trigger is inside it, and leaving both open
      // would stack two panels on top of each other.
      document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
      if (!isOpen) {
        // Anchor the sub-panel to the kebab BUTTON, not to this menu row, so it
        // lands where the first panel was rather than halfway down the screen.
        const anchor = taskMenuBtn || restartSummaryBtn;
        const rect = anchor.getBoundingClientRect();
        restartSummaryMenu.style.top = `${rect.bottom + 4}px`;
        restartSummaryMenu.style.left = "0px";
        restartSummaryMenu.classList.add("open");
        restartSummaryMenu.style.left = `${Math.max(8, rect.right - restartSummaryMenu.offsetWidth)}px`;
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

  root._elements = {
    linkEl: root.querySelector(".task-link"),
    playerBtn: root.querySelector(".task-player-btn"),
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
    resolveVoicesBtn,
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
  return root;
}

function appendTaskCard(task) {
  if (findTaskEl(task.id)) return;
  taskList.appendChild(renderTaskCard(task));
}

function prependTaskCard(task) {
  if (findTaskEl(task.id)) return;
  taskList.insertBefore(renderTaskCard(task), taskList.firstChild);
}

function renderTasks(tasks) {
  stopAllLogPolling();
  taskList.innerHTML = "";
  tasks.forEach((task) => appendTaskCard(task));
  updateQueueWatcher(tasks);
  if (typeof updateEmptyState === "function") updateEmptyState();
}

function cursorOf(taskEl) {
  if (!taskEl) return null;
  return { ts: taskEl.dataset.createdAt, id: taskEl.dataset.taskId };
}

function updateHeadTail() {
  const cards = taskList.querySelectorAll(".task");
  state.taskPaging.head = cursorOf(cards[0]);
  state.taskPaging.tail = cursorOf(cards[cards.length - 1]);
}

function updateSentinel() {
  const sentinel = document.getElementById("task-sentinel");
  if (!sentinel) return;
  const p = state.taskPaging;
  sentinel.hidden = false;
  sentinel.querySelector(".task-sentinel-spinner").hidden = !p.loading;
  sentinel.querySelector(".task-sentinel-end").hidden = !p.exhausted;
}

function isNewerThan(ts, id, cursor) {
  // returns true if (ts,id) > cursor lexicographically on (ts, id)
  if (!cursor) return true;
  if (ts > cursor.ts) return true;
  if (ts < cursor.ts) return false;
  return String(id) > String(cursor.id);
}

function forgetOwnTask(taskId) {
  // The claim only needs to outlive the create round-trip: once the card is in
  // the DOM, findTaskEl() is what keeps the banner quiet. Dropping it keeps
  // ownIds from growing for the life of the tab. The delay covers a
  // task_status event that raced just behind the HTTP response.
  window.setTimeout(() => {
    state.taskPaging.ownIds.delete(String(taskId));
  }, 30_000);
}

function clearNewTasksBanner() {
  state.taskPaging.newIds.clear();
  const b = document.getElementById("new-tasks-banner");
  if (b) b.hidden = true;
}

function showNewTasksBanner() {
  const b = document.getElementById("new-tasks-banner");
  const c = document.getElementById("new-tasks-count");
  if (!b) return;
  const n = state.taskPaging.newIds.size;
  if (n === 0) { b.hidden = true; return; }
  if (c) c.textContent = `(${n})`;
  b.hidden = false;
}

async function maybeFlagNewerTask(taskId) {
  const p = state.taskPaging;
  // Never announce this tab's own creation as somebody else's new task: its
  // card is on its way in from the create/upload response (vts-3iw).
  if (p.ownIds.has(taskId)) return;
  if (p.newIds.has(taskId) || findTaskEl(taskId)) return;
  let task;
  try {
    task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch {
    return;
  }
  if (!task || !task.created_at) return;
  // Re-check after the await: the task may have been pulled into the DOM by
  // loadNewer()/prepend while /api/tasks/{id} was in flight. Without this,
  // its id would still be added to newIds, inflating the banner count for a
  // task that is already visible (vts-7ud).
  if (p.newIds.has(taskId) || findTaskEl(taskId)) return;
  if (isNewerThan(task.created_at, task.id, p.head)) {
    p.newIds.add(taskId);
    showNewTasksBanner();
  }
}

async function loadNewer() {
  const p = state.taskPaging;
  if (p.loading || !p.head) return;
  const myEpoch = p.epoch;
  p.loading = true;
  const q = appendFilterParams(new URLSearchParams({
    limit: String(p.pageSize),
    order: "asc",
    after_ts: p.head.ts,
    after_id: p.head.id,
  }));
  try {
    let tasks;
    try {
      tasks = await api(`/api/tasks?${q.toString()}`);
    } catch {
      return;
    }
    // A loadFirstPage reset may have happened while this fetch was in
    // flight (same guard loadNextPage uses): discard the stale result
    // instead of prepending onto a list that's been rebuilt/invalidated.
    if (myEpoch !== p.epoch) return;
    // ASC from server → reverse so newest ends on top after successive prepends
    tasks.slice().reverse().forEach((t) => prependTaskCard(t));
    updateHeadTail();
    // Drop now-loaded ids; if a full page came back there may be more above.
    tasks.forEach((t) => p.newIds.delete(t.id));
    if (tasks.length < p.pageSize) {
      clearNewTasksBanner();
    } else {
      showNewTasksBanner();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  } finally {
    // Only the call that owns the current epoch clears `loading`, matching
    // loadFirstPage/loadNextPage's ownership pattern.
    if (myEpoch === p.epoch) {
      p.loading = false;
    }
  }
}

document.getElementById("new-tasks-banner")
  ?.addEventListener("click", () => void loadNewer());

async function loadFirstPage() {
  const p = state.taskPaging;
  // No re-entrancy guard on purpose: a reset must always be able to
  // interrupt an in-flight loadNextPage (or a stale loadFirstPage) rather
  // than be blocked by it. Bumping epoch here invalidates any fetch that
  // was already in flight — its result gets discarded when it resolves
  // (see the epoch check below and in loadNextPage).
  const myEpoch = ++p.epoch;
  p.loading = true;
  // clearNewTasksBanner() is defined in Task 6; guard until it lands.
  if (typeof clearNewTasksBanner === "function") clearNewTasksBanner();
  updateSentinel();
  try {
    let tasks;
    try {
      const q1 = appendFilterParams(new URLSearchParams({ limit: String(p.pageSize) }));
      tasks = await api(`/api/tasks?${q1.toString()}`);
    } catch (err) {
      if (myEpoch !== p.epoch) return; // superseded by a newer reset; don't clobber its DOM
      taskList.textContent = err.message;
      return;
    }
    if (myEpoch !== p.epoch) return; // a newer loadFirstPage already reset the list
    renderTasks(tasks);
    p.exhausted = tasks.length < p.pageSize;
    updateHeadTail();
  } finally {
    // Only the call that owns the current epoch clears `loading`/repaints
    // the sentinel; a stale call must not clobber a newer call's state.
    if (myEpoch === p.epoch) {
      p.loading = false;
      updateSentinel();
    }
  }
}

async function loadNextPage() {
  const p = state.taskPaging;
  if (p.loading || p.exhausted || !p.tail) return;
  const myEpoch = p.epoch;
  p.loading = true;
  updateSentinel();
  const q = appendFilterParams(new URLSearchParams({
    limit: String(p.pageSize),
    order: "desc",
    before_ts: p.tail.ts,
    before_id: p.tail.id,
  }));
  try {
    let tasks;
    try {
      tasks = await api(`/api/tasks?${q.toString()}`);
    } catch {
      return;
    }
    if (myEpoch !== p.epoch) return; // a reset happened while this fetch was in flight; discard
    tasks.forEach((t) => appendTaskCard(t));
    p.exhausted = tasks.length < p.pageSize;
    updateHeadTail();
  } finally {
    if (myEpoch === p.epoch) {
      p.loading = false;
      updateSentinel();
    }
  }
}

async function loadTasks() {
  await loadFirstPage();
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
  // The native input stays hidden in both modes: the drop zone's Choose / Add
  // more buttons open it, and the staged rows are what the user actually sees.
  const fileDrop = document.getElementById("file-drop");
  if (isFile) {
    urlInput.classList.add("hidden");
    urlInput.required = false;
    fileDrop?.classList.remove("hidden");
    fileInput.required = stagedFiles.length === 0;
    if (audioOnlyPill) audioOnlyPill.classList.add("hidden");
  } else {
    urlInput.classList.remove("hidden");
    urlInput.required = true;
    fileDrop?.classList.add("hidden");
    fileInput.required = false;
    if (audioOnlyPill) audioOnlyPill.classList.remove("hidden");
  }
}

// --- Upload progress toast -------------------------------------------------
//
// Diana: "неочевидно, сколько файлов загрузилось... сама загрузка не наглядная".
// The submit button's ring is a single indeterminate-looking arc; this shows
// the task-style pattern instead — a top bar for the set (N of M files) and a
// bottom bar for the chunks of the file currently in flight.
//
// One shared controller for all three upload paths, so a single-shot upload,
// a chunked single file and a multi-file session all report the same way.
// A single file drops the FILES bar entirely (`.single`) and keeps only its own
// progress under the filename: with nothing to count, two bars would show the
// same number twice.
const uploadToast = {
  el: document.getElementById("upload-toast"),
  titleEl: document.getElementById("upload-toast-title"),
  countEl: document.getElementById("upload-toast-count"),
  filesFill: document.getElementById("upload-toast-files-fill"),
  filesText: document.getElementById("upload-toast-files-text"),
  filesBar: document.getElementById("upload-toast-files-bar"),
  fileNameEl: document.getElementById("upload-toast-filename"),
  chunksFill: document.getElementById("upload-toast-chunks-fill"),
  chunksText: document.getElementById("upload-toast-chunks-text"),
  chunksBar: document.getElementById("upload-toast-chunks-bar"),
  total: 0,

  // total = number of files in this upload. A total of 1 hides the files bar
  // and leaves the per-file one.
  start(total) {
    if (!this.el) {
      return;
    }
    this.total = Math.max(0, Number(total) || 0);
    this.el.classList.toggle("single", this.total <= 1);
    if (this.titleEl) {
      this.titleEl.textContent = t("upload.toast.title");
    }
    this.setFiles(0, 0);
    this.setChunks(0);
    if (this.fileNameEl) {
      this.fileNameEl.textContent = "";
    }
    this.el.hidden = false;
  },

  // done = files fully sent, ratio = fraction of total BYTES sent (smoother
  // than done/total, which would sit still through a large file).
  setFiles(done, ratio) {
    if (!this.el) {
      return;
    }
    const pct = Math.max(0, Math.min(100, Math.round((Number(ratio) || 0) * 100)));
    if (this.filesFill) {
      this.filesFill.style.width = `${pct}%`;
    }
    if (this.filesText) {
      this.filesText.textContent = `${pct}%`;
    }
    if (this.filesBar) {
      this.filesBar.setAttribute("aria-valuenow", String(pct));
    }
    if (this.countEl) {
      this.countEl.textContent = this.total > 1
        ? t("upload.toast.files", { done: Math.min(done, this.total), total: this.total })
        : "";
    }
  },

  setCurrentFile(name) {
    if (this.fileNameEl) {
      this.fileNameEl.textContent = String(name || "");
    }
  },

  setChunks(ratio) {
    if (!this.el) {
      return;
    }
    const pct = Math.max(0, Math.min(100, Math.round((Number(ratio) || 0) * 100)));
    if (this.chunksFill) {
      this.chunksFill.style.width = `${pct}%`;
    }
    if (this.chunksText) {
      this.chunksText.textContent = `${pct}%`;
    }
    if (this.chunksBar) {
      this.chunksBar.setAttribute("aria-valuenow", String(pct));
    }
  },

  stop() {
    if (this.el) {
      this.el.hidden = true;
    }
  },
};

function uploadFileWithProgress(fd, fileName) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  if (fill) fill.style.strokeDashoffset = circumference;
  uploadToast.start(1);
  uploadToast.setCurrentFile(fileName || "");

  function setProgress(ratio) {
    if (fill) fill.style.strokeDashoffset = circumference * (1 - ratio);
    // Single file: the files bar is hidden by `.single`, so the byte progress
    // goes on the per-file bar under the filename.
    uploadToast.setChunks(ratio);
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
        let task = null;
        try { task = JSON.parse(xhr.responseText); } catch (_) {}
        resolve(task);
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
    uploadToast.stop();
  });
}

async function uploadFileChunked(file, fields) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;
  const setProgress = (r) => {
    if (fill) fill.style.strokeDashoffset = circumference * (1 - r);
    // One file: the files bar is hidden by `.single`; the byte progress goes
    // on the per-file bar under the filename.
    uploadToast.setChunks(r);
  };
  // Declared out here so the catch below can release the ownIds claim.
  let uploadId = null;

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  uploadToast.start(1);
  uploadToast.setCurrentFile(file.name);
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
        diarize: fields.diarize,
        prompts: fields.prompts,
        delivery: fields.delivery,
        display_name: fields.display_name || null,
      }),
      headers: {
        "Content-Type": "application/json",
        "X-Forwarded-User": state.authUser,
      },
    });
    uploadId = init.upload_id;
    // The finalize endpoint creates the task with task_id = upload_id, so we
    // know our own task's id before it exists. Claim it now: uploading the
    // chunks can take minutes, and the server publishes task_status before
    // returning the finalize response, so the SSE event would otherwise
    // announce this tab's own upload as somebody else's new task (vts-3iw).
    state.taskPaging.ownIds.add(uploadId);
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
    // Return the created task: the caller prepends it straight onto the list.
    // Discarding it left `created` null, so the chunked path fell through to
    // loadFirstPage() and the SSE task_status event — which arrives first —
    // flagged the user's own upload as "new tasks (1)" instead of showing the
    // card (vts-3iw). The single-shot path already returned its task.
    const task = await api(`/api/uploads/${uploadId}/finalize`, {
      method: "POST",
      headers: { "X-Forwarded-User": state.authUser },
    });
    setProgress(1);
    return task;
  } catch (err) {
    // Release the claim made at init: with no task to render, nothing else
    // would ever drop it (vts-3iw).
    if (uploadId) state.taskPaging.ownIds.delete(uploadId);
    throw err;
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
    // finally, not the success path: a failed or aborted upload must not leave
    // the toast pinned to the corner forever.
    uploadToast.stop();
  }
}

async function uploadFilesChunked(files, fields) {
  const btn = document.getElementById("submit-btn");
  const icon = btn && btn.querySelector(".submit-icon");
  const ring = btn && btn.querySelector(".submit-progress");
  const fill = ring && ring.querySelector(".submit-progress-fill");
  const circumference = 56.55;
  // Aggregate progress: sum of bytes sent across the whole set over the sum
  // of all file sizes, so the ring advances monotonically across files
  // instead of restarting at 0 each time a file finishes (vts-vm0).
  const grandTotal = files.reduce((sum, f) => sum + f.size, 0);
  let sentBefore = 0; // bytes confirmed sent for files completed so far
  const setProgress = (r) => { if (fill) fill.style.strokeDashoffset = circumference * (1 - r); };
  // The toast's two bars. `done` is how many files are fully sent; the top bar
  // still advances by BYTES, so it keeps moving through a single large file
  // instead of freezing between whole-file steps.
  const setToast = (index, fileOffset, fileSize) => {
    uploadToast.setFiles(index, grandTotal ? (sentBefore + fileOffset) / grandTotal : 1);
    uploadToast.setChunks(fileSize ? fileOffset / fileSize : 1);
  };
  // Declared out here so the catch below can release the ownIds claim.
  let uploadId = null;

  if (btn) btn.disabled = true;
  if (icon) icon.classList.add("hidden");
  if (ring) ring.classList.remove("hidden");
  uploadToast.start(files.length);
  setProgress(0); // determinate from the start

  try {
    const init = await api("/api/uploads/init", {
      method: "POST",
      body: JSON.stringify({
        files: files.map((f) => ({
          filename: f.name,
          total_size: f.size,
          last_modified: f.lastModified || null,
        })),
        language: fields.language || null,
        audio_only: fields.audio_only,
        transcript: fields.transcript,
        diarize: fields.diarize,
        prompts: fields.prompts,
        delivery: fields.delivery,
        display_name: fields.display_name || null,
      }),
      headers: {
        "Content-Type": "application/json",
        "X-Forwarded-User": state.authUser,
      },
    });
    uploadId = init.upload_id;
    // Same "claim before the SSE event can arrive" reasoning as the
    // single-file chunked path above (vts-3iw / vts-vm0).
    state.taskPaging.ownIds.add(uploadId);
    const chunkSize = init.chunk_size || 8388608;

    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      let offset = 0;
      uploadToast.setCurrentFile(file.name);
      setToast(index, 0, file.size);
      while (offset < file.size) {
        const slice = file.slice(offset, Math.min(offset + chunkSize, file.size));
        const buf = await slice.arrayBuffer();
        let resp;
        try {
          resp = await api(`/api/uploads/${uploadId}?offset=${offset}&index=${index}`, {
            method: "PATCH",
            body: buf,
            headers: {
              "Content-Type": "application/offset+octet-stream",
              "X-Forwarded-User": state.authUser,
            },
          });
        } catch (err) {
          // On offset conflict or transient error, re-sync from the server.
          const off = await api(`/api/uploads/${uploadId}/offset?index=${index}`, {
            headers: { "X-Forwarded-User": state.authUser },
          });
          offset = off.received;
          setProgress(grandTotal ? (sentBefore + offset) / grandTotal : 1);
          setToast(index, offset, file.size);
          continue;
        }
        offset = resp.received;
        setProgress(grandTotal ? (sentBefore + offset) / grandTotal : 1);
        setToast(index, offset, file.size);
      }
      sentBefore += file.size;
      // Whole file done: reflect it in the "N of M" counter right away.
      uploadToast.setFiles(index + 1, grandTotal ? sentBefore / grandTotal : 1);
    }

    // Return the created task: the caller prepends it straight onto the list,
    // same as the single-file chunked path (vts-3iw).
    const task = await api(`/api/uploads/${uploadId}/finalize`, {
      method: "POST",
      headers: { "X-Forwarded-User": state.authUser },
    });
    setProgress(1);
    return task;
  } catch (err) {
    // Release the claim made at init: with no task to render, nothing else
    // would ever drop it (vts-3iw).
    if (uploadId) state.taskPaging.ownIds.delete(uploadId);
    throw err;
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.classList.remove("hidden");
    if (ring) ring.classList.add("hidden");
    // finally, not the success path: a failed or aborted upload must not leave
    // the toast pinned to the corner forever.
    uploadToast.stop();
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
    diarize: !!form.diarize.checked,
    speaker_no_manual_stop: !!form.speaker_no_manual_stop.checked,
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
    !!oa.diarize === !!ob.diarize &&
    !!oa.speaker_no_manual_stop === !!ob.speaker_no_manual_stop &&
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
  form.diarize.checked = !!opts.diarize;
  form.speaker_no_manual_stop.checked = !!opts.speaker_no_manual_stop;
  const { filtered, dangling } = filterDanglingPrompts(opts.prompts);
  if (promptSelect) {
    renderPromptMultiselect(promptSelect, promptsCache, filtered);
  }
  syncSummaryToggle();
  // A preset that names destinations should select them here too, otherwise
  // picking the preset would silently drop the delivery it was saved with.
  renderDeliveryMultiselect(
    deliverySelect,
    (opts.delivery || []).filter((d) =>
      deliveryTargetsList().some((t) => t.id === d.deliver_to)
    )
  );
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
form.diarize.addEventListener("change", recomputePresetDirty);
form.diarize.addEventListener("change", syncSpeakerNoManualStopToggle);
form.speaker_no_manual_stop.addEventListener("change", recomputePresetDirty);
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
  let created = null;
  try {
    if (isFile && fileInput) {
      const selected = stagedFiles.length ? stagedFiles.slice() : Array.from(fileInput.files || []);
      if (!selected.length) {
        showTaskFormError(t("upload.file_unreadable"));
        return;
      }
      // Probe one byte of each before starting: a stale file reference fails
      // here with a clear message instead of mid-upload (covers the
      // single-shot XHR path, which reads the file natively and only reports
      // a generic network error).
      for (const file of selected) {
        await file.slice(0, 1).arrayBuffer();
      }
      // audio_only is a yt-dlp download hint: DownloadStep skips the download
      // entirely for an uploaded file, so the flag is meaningless here. Drop it
      // at the boundary rather than clearing the control — the form keeps the
      // user's choice for presets and for switching back to a URL source.
      const fields = {
        language: form.language.value || "",
        audio_only: false,
        transcript: form.transcript.checked,
        diarize: form.diarize.checked,
        prompts: JSON.stringify(getSelectedPrompts()),
        delivery: JSON.stringify(selectedDeliveryRefs(deliverySelect)),
        display_name: "",
      };
      if (selected.length > 1) {
        // A set of 2+ files is always a multi-file recording: upload it as
        // one session with per-file chunking (vts-vm0). Only a single
        // selection takes the existing single-shot/chunked-by-threshold path.
        created = await uploadFilesChunked(selected, fields);
      } else {
        const file = selected[0];
        const threshold = uploadConfig && Number.isFinite(uploadConfig.chunked_threshold_bytes)
          ? uploadConfig.chunked_threshold_bytes
          : Infinity; // no config -> always single-shot (unchanged behavior)
        if (file.size > threshold) {
          created = await uploadFileChunked(file, fields);
        } else {
          const fd = new FormData();
          fd.append("file", file);
          if (fields.language) fd.append("language", fields.language);
          fd.append("audio_only", fields.audio_only ? "true" : "false");
          fd.append("transcript", fields.transcript ? "true" : "false");
          fd.append("diarize", fields.diarize ? "true" : "false");
          fd.append("prompts", fields.prompts);
          fd.append("delivery", fields.delivery);
          created = await uploadFileWithProgress(fd, file.name);
        }
      }
    } else {
      const payload = {
        url: form.url.value,
        language: form.language.value || null,
        audio_only: form.audio_only.checked,
        transcript: form.transcript.checked,
        diarize: form.diarize.checked,
        speaker_no_manual_stop: form.speaker_no_manual_stop.checked,
        prompts: getSelectedPrompts(),
        delivery: selectedDeliveryRefs(deliverySelect)
      };
      created = await api("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    }
  } catch (err) {
    if (isFileReadError(err)) {
      if (fileInput) fileInput.value = "";
      clearStagedFiles();
      showTaskFormError(t("upload.file_unreadable"));
    } else {
      const message = err && err.message ? err.message : String(err);
      showTaskFormError(t("upload.failed", { message }));
    }
    return;
  }
  form.reset();
  clearStagedFiles();
  form.transcript.checked = true;
  resetPromptSelection();
  resetDeliverySelection();
  syncSummaryToggle();
  syncSourceType();
  if (created && created.id) {
    // Claim it before rendering, so a task_status event still in flight for
    // this task cannot flag it as new (vts-3iw).
    state.taskPaging.ownIds.add(String(created.id));
    // A newly submitted task can fall outside the active filter; showing it
    // anyway would put a row in the list that a reload then removes.
    if (typeof taskMatchesFilters !== "function" || taskMatchesFilters(created)) {
      prependTaskCard(created);
    }
    updateHeadTail();
    forgetOwnTask(created.id);
    void refreshQueuePositions();
  } else {
    await loadFirstPage();
  }
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
  // Diarization labels transcript segments, so it cannot run without one — the
  // API rejects that pair outright ("diarize requires transcript"). Dim it like
  // the language control, and for the same reason never clear the checkbox:
  // currentFormOptions() reads it, so clearing would mark a preset dirty (vts-86k).
  const diarizePill = document.getElementById("diarize-pill");
  if (diarizePill) {
    diarizePill.classList.toggle("disabled", disabled);
  }
  form.diarize.disabled = disabled;
  syncSpeakerNoManualStopToggle();
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

// "Don't stop for review" only means anything when diarize actually runs (the
// API rejects speaker_no_manual_stop without diarize). Never clear the value
// on disable — same reasoning as diarize itself (vts-86k): currentFormOptions()
// reads it directly, so clearing would mark a preset dirty on a mere toggle.
function syncSpeakerNoManualStopToggle() {
  const disabled = !form.diarize.checked || form.diarize.disabled;
  const pill = document.getElementById("speaker-no-manual-stop-pill");
  if (pill) {
    pill.classList.toggle("disabled", disabled);
  }
  form.speaker_no_manual_stop.disabled = disabled;
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
  // awaiting_input is resumable without the dialog (can_resume stays true —
  // blocking would only add clicks for a user who wants to bind nothing), but
  // it carries a consequence: any voice never resolved stays anonymous. That
  // must be confirmed here, not just inside the dialog's own save&continue.
  const taskEl = findTaskEl(taskId);
  const status = taskEl && taskEl._runtime ? taskEl._runtime.baseStatus : "";
  if (status === "awaiting_input" && !window.confirm(t("confirm.resume_awaiting_input"))) {
    return;
  }
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

// Resync after the SSE stream dropped and came back, WITHOUT rebuilding the
// list. While the stream was down we missed events, so what is on screen may
// be stale — but loadFirstPage() fixes that by destroying the view: it does
// `taskList.innerHTML = ""`, so every expanded card collapses, its open tab is
// lost, and pages scrolled in via infinite scroll are dropped back to the
// first page. On a flaky connection that fired every couple of seconds
// (vts-9zs).
//
// Instead, refresh the cards that are actually on screen in place — the same
// approach refreshTaskInPlace() already uses for a single task — and pull in
// anything created while we were disconnected via the existing loadNewer()
// path, which prepends rather than rebuilds.
async function resyncAfterReconnect() {
  const cards = Array.from(document.querySelectorAll(".task"));
  if (!cards.length) {
    // Nothing on screen to preserve, so the cheap path is also the correct
    // one (first load, or the list genuinely empty).
    await loadFirstPage();
    return;
  }
  // Re-read the first page and patch the matching cards in place. Deliberately
  // NOT refreshTaskInPlace() per card: that helper blanks runtime.awaitingStep
  // when the response omits the field, which is safe for the one task it was
  // written for but would silently disable controls (the resolve-voices
  // button) across the whole list if any response came back partial. Patching
  // from the list keeps the cards' DOM — and their expanded state — intact.
  let fresh;
  try {
    fresh = await api(`/api/tasks?limit=${state.taskPaging.pageSize}`);
  } catch {
    return; // still offline; the next reconnect will try again
  }
  if (!Array.isArray(fresh)) return;
  fresh.forEach((task) => {
    if (!task || !task.id) return;
    const el = findTaskEl(task.id);
    if (!el || !el._runtime) return;
    patchTaskStatus(
      task.id,
      task.status,
      task.error || "",
      task.failure_code || "",
      task.queue,
      typeof task.awaiting_step === "string" ? task.awaiting_step : undefined,
    );
  });
  // Anything on the fresh page we don't have a card for was created while the
  // stream was down; prepend it rather than rebuilding.
  const known = new Set(cards.map((el) => el.dataset.taskId));
  fresh
    .filter((task) => task && task.id && !known.has(String(task.id)))
    .reverse()
    .forEach((task) => prependTaskCard(task));
  updateHeadTail();
  void refreshQueuePositions();
}

// Refresh ONE task's runtime from the server and re-render it in place, WITHOUT
// rebuilding the task list. loadTasks() does `taskList.innerHTML = ""`, which
// recreates every card collapsed and loses the open transcript tab; this keeps
// the card's expanded/tab state intact (bug #3, vts-552). Mirrors the in-place
// patch patchTaskStatus already does for completed/failed tasks.
async function refreshTaskInPlace(taskId) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) return;
  let task;
  try {
    task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
  } catch {
    return;
  }
  const runtime = taskEl._runtime;
  if (taskEl._runtime !== runtime || !task) return;
  runtime.baseStatus = String(task.status || runtime.baseStatus);
  runtime.awaitingStep = typeof task.awaiting_step === "string" ? task.awaiting_step : "";
  if (task.capabilities) runtime.capabilities = task.capabilities;
  if (task.stats) runtime.stats = parseTaskStats(task);
  runtime.mediaReady = Boolean(task.media_path);
  runtime.transcriptReady = Boolean(task.transcript_path);
  runtime.summaryReady = Boolean(task.summary_path);
  if (task.options && Array.isArray(task.options.prompt_results)) {
    runtime.promptResults = task.options.prompt_results;
  }
  renderTaskRuntime(taskEl);
}

function patchTaskStatus(taskId, status, errorMessage = "", failureCode = "", queue = undefined, awaitingStep = undefined) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  runtime.baseStatus = String(status || "");
  // The backend emits awaiting_step alongside the status when a task pauses for
  // review (processor.py). Without copying it here, runtime.awaitingStep kept
  // its stale value from the last full load, so the resolve-voices button's
  // gate (awaitingStep === "match_speakers") stayed false and the button never
  // appeared until a page reload (vts-552). Only overwrite when the event
  // actually carries the field, so a plain status event (running, completed)
  // does not blank a value a prior awaiting_input event set.
  if (awaitingStep !== undefined) {
    runtime.awaitingStep = typeof awaitingStep === "string" ? awaitingStep : "";
  }
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
    // Fold the just-finished (failed) step's live duration into the running
    // total so the work-time timer stays continuous. Skipped steps take no
    // time, so they are not folded in.
    if (runtime.currentStepName === stepName && runtime.currentStepStartedAt) {
      const stepMs = Date.now() - runtime.currentStepStartedAt;
      if (stepMs > 0) {
        runtime.completedStepMs = (runtime.completedStepMs || 0) + stepMs;
      }
      runtime.currentStepStartedAt = null;
    }
  } else if (stepStatus === "completed" || stepStatus === "skipped") {
    if (runtime.currentStepName === stepName) {
      // Accumulate this step's own duration into the work-time total before
      // clearing the running marker, so the timer keeps summing per-step
      // durations across the whole run (and never counts the idle gap before
      // the next step starts). Mirrors computeCompletedStepMs.
      if (stepStatus === "completed" && runtime.currentStepStartedAt) {
        const stepMs = Date.now() - runtime.currentStepStartedAt;
        if (stepMs > 0) {
          runtime.completedStepMs = (runtime.completedStepMs || 0) + stepMs;
        }
      }
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

function patchDiarizeProgress(taskId, step, current, total) {
  const taskEl = findTaskEl(taskId);
  if (!taskEl || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  // The step matters: only "embeddings" carries a total, so the render branch
  // reads it to decide between a percentage and a running indicator.
  runtime.diarize.step = String(step || "");
  runtime.diarize.current = Number(current) || 0;
  runtime.diarize.total = Number(total) || 0;
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
    patchTaskStatus(payload.task_id, payload.data.status, payload.data.error, payload.data.failure_code, payload.data.queue, payload.data.awaiting_step);
    if (!findTaskEl(payload.task_id)) {
      void maybeFlagNewerTask(payload.task_id);
    }
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
  state.eventSource.addEventListener("diarize_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchDiarizeProgress(
      payload.task_id,
      payload.data.step,
      payload.data.completed,
      payload.data.total
    );
  });
  state.eventSource.addEventListener("transcript_segment_text", (event) => {
    const payload = JSON.parse(event.data);
    appendTranscriptSegment(payload.task_id, payload.data.text);
  });
  state.eventSource.addEventListener("segment_summary_text", (event) => {
    const payload = JSON.parse(event.data);
    appendRedactedSegment(payload.task_id, payload.data.text);
  });
  // Universal "transcript is whole again" signal (vts-at8): fired on first
  // assembly (merge_transcript) AND on rerender after speaker resolve/save.
  // Re-render the raw transcript in place if that tab is open — this is the
  // path that keeps OTHER open tabs/sessions in sync; the resolve/save
  // initiator also refreshes locally (idempotent, just faster for them).
  state.eventSource.addEventListener("transcript_updated", (event) => {
    const payload = JSON.parse(event.data);
    const taskEl = findTaskEl(payload.task_id);
    if (taskEl && getActiveTabName(taskEl) === "transcript") {
      void loadTabContent(taskEl, payload.task_id, "transcript");
    }
  });
  // The server is stopping (deploy or restart) and says so before closing the
  // stream, so we come back deliberately instead of waiting out onerror's
  // blind 2s backoff. Nulling state.eventSource first also stops onerror —
  // which fires as the stream drops — from scheduling a second reconnect.
  // A shutdown almost always means a new version is landing; the reconnect's
  // server_version frame triggers the reload when it does.
  state.eventSource.addEventListener("server_shutdown", () => {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    setTimeout(() => {
      connectEvents();
      void resyncAfterReconnect();
    }, 1000);
  });

  state.eventSource.onerror = () => {
    if (!state.eventSource) {
      // Already handled — server_shutdown got here first and has queued its
      // own reconnect. Doing it again would open two streams.
      return;
    }
    state.eventSource.close();
    state.eventSource = null;
    setTimeout(() => {
      connectEvents();
      void resyncAfterReconnect();
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
  // The admin marker is a badge now, not a suffix glued onto the username:
  // appending it meant the mono value contained prose, and it could not be
  // styled apart from the address it followed.
  authUserLabel.textContent = String(me.requested_by || "");
  document.getElementById("auth-admin-badge")?.classList.toggle("hidden", !me.is_admin);
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
    if (cfg && Number.isFinite(cfg.tasks_page_size)) {
      state.taskPaging.pageSize = cfg.tasks_page_size;
    }
  } catch { /* predicates degrade to false; loadTasks still renders */ }
  await loadTasks();
  connectEvents();
  startVersionWatcher();
  startDurationTicker();
}

// Entries that change state in place instead of opening a dialog: clicking one
// must NOT close the menu, or cycling the theme through its three states means
// reopening the menu twice. Everything else closes it, including a click on the
// page background.
const MENU_KEEPS_OPEN = new Set(["theme-toggle-btn", "locale-toggle-btn", "push-toggle-btn"]);

document.addEventListener("click", (event) => {
  const keepOpen =
    event.target instanceof Element &&
    MENU_KEEPS_OPEN.has(event.target.closest("button")?.id || "");
  if (!keepOpen) {
    document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
    const hdrBtn = document.getElementById("header-menu-btn");
    if (hdrBtn) hdrBtn.setAttribute("aria-expanded", "false");
  }
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

// Tooltips reveal on :focus (deliberately, so touch taps — which have no hover —
// can still show them). But a modal <dialog> moves focus PROGRAMMATICALLY twice,
// and both fire an unwanted tooltip that the user never asked for:
//   - on OPEN, showModal() autofocuses the dialog's first control (the ✕ close
//     button), whose tooltip then flashes clipped at the dialog's top edge;
//   - on CLOSE, focus returns to the trigger that opened the dialog, whose
//     tooltip then hangs until the next click (the "stuck tooltip" screenshot).
// Blur a data-tooltip button that receives such a programmatic focus, but only
// when it is NOT :focus-visible — a real keyboard user (roving focus ring) keeps
// their tooltip, and a later touch tap (outside the just-changed window) keeps
// working too.
function blurIfProgrammaticTooltipFocus(el) {
  if (!(el instanceof HTMLElement)) return;
  if (!el.hasAttribute("data-tooltip")) return;
  let focusVisible = false;
  try {
    focusVisible = el.matches(":focus-visible");
  } catch {
    // :focus-visible unsupported — treat as keyboard focus and leave it be.
    return;
  }
  if (!focusVisible) el.blur();
}

let dialogFocusGuardUntil = 0;
function markDialogFocusGuard() {
  // performance.now avoids Date.now; cleared next frame, so only the synchronous
  // programmatic focus change (open autofocus / close refocus) falls inside it.
  dialogFocusGuardUntil = performance.now() + 100;
  requestAnimationFrame(() => { dialogFocusGuardUntil = 0; });
}

if (typeof HTMLDialogElement !== "undefined") {
  // Wrap both open and close: showModal() autofocuses on open, close() returns
  // focus to the trigger — both synchronous, so marking the guard BEFORE calling
  // native puts that focus change inside the window. (The `close` EVENT fires
  // AFTER focus has already returned, too late to guard from there.)
  for (const name of ["showModal", "close", "requestClose"]) {
    const native = HTMLDialogElement.prototype[name];
    if (typeof native !== "function") continue;
    HTMLDialogElement.prototype[name] = function patchedDialogMethod(...args) {
      markDialogFocusGuard();
      return native.apply(this, args);
    };
  }
}

document.addEventListener("focusin", (event) => {
  if (performance.now() > dialogFocusGuardUntil) return;
  blurIfProgrammaticTooltipFocus(event.target);
});

refreshBtn.addEventListener("click", loadTasks);
form.addEventListener("submit", createTask);
// ---------------------------------------------------------------------------
// Staged file selection.
//
// fileInput.files is a read-only FileList: you cannot drop one entry from it,
// and a fresh pick REPLACES it rather than appending. So the staged File[] here
// is the source of truth, and the input is rebuilt from it via DataTransfer
// (the same trick the share-target handler uses) purely so that native form
// semantics and the existing submit path keep working unchanged.
//
// Order is significant: several files are concatenated in extract_audio, so the
// row number is always shown and the rows can be reordered by dragging.
const stagedFiles = [];

const fileDropEl = document.getElementById("file-drop");
const fileListEl = document.getElementById("file-list");
const fileFootEl = document.getElementById("file-foot");
const fileFootTextEl = document.getElementById("file-foot-text");
const fileEmptyEl = document.getElementById("file-drop-empty");
const fileWarningEl = document.getElementById("file-warning");

function formatFileSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

// Identity for dedupe: name + size. Two different recordings can share a name,
// but not a name AND an exact byte count; this is the strongest check available
// without reading the files.
const fileKey = (f) => `${f.name}::${f.size}`;

function showFileWarning(message) {
  if (!fileWarningEl) return;
  fileWarningEl.textContent = message;
  fileWarningEl.classList.toggle("hidden", !message);
}

// Keep the native input in step with the staged array so the existing submit
// path, form.reset() and required-validation all keep working.
function syncFileInput() {
  const input = document.getElementById("file-input");
  if (!input) return;
  const dt = new DataTransfer();
  for (const f of stagedFiles) dt.items.add(f);
  input.files = dt.files;
  input.required = stagedFiles.length === 0 && getSourceType() === "file";
}

function renderStagedFiles() {
  if (!fileListEl || !fileFootEl || !fileEmptyEl) return;
  const has = stagedFiles.length > 0;
  fileEmptyEl.classList.toggle("hidden", has);
  fileListEl.classList.toggle("hidden", !has);
  fileFootEl.classList.toggle("hidden", !has);
  fileListEl.textContent = "";

  stagedFiles.forEach((file, index) => {
    const row = document.createElement("div");
    row.className = "file-row";
    row.setAttribute("role", "listitem");
    row.draggable = true;
    row.dataset.index = String(index);

    const num = document.createElement("span");
    num.className = "file-row-num mono";
    num.textContent = String(index + 1);

    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    const p1 = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p1.setAttribute("d", "M15 4H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z");
    const p2 = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p2.setAttribute("d", "M14 4v5h5");
    icon.append(p1, p2);

    const name = document.createElement("span");
    name.className = "file-row-name";
    name.textContent = file.name;
    name.title = file.name;

    const size = document.createElement("span");
    size.className = "file-row-size mono";
    size.textContent = formatFileSize(file.size);

    // Keyboard path for reordering: dragging alone would make ordering — which
    // changes the concatenated output — mouse-only.
    const up = document.createElement("button");
    up.type = "button";
    up.className = "icon-btn ghost file-row-move";
    up.disabled = index === 0;
    up.setAttribute("aria-label", t("new_task.file_move_up"));
    up.setAttribute("data-tooltip", t("new_task.file_move_up"));
    up.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    up.addEventListener("click", () => moveStagedFile(index, index - 1));

    const down = document.createElement("button");
    down.type = "button";
    down.className = "icon-btn ghost file-row-move";
    down.disabled = index === stagedFiles.length - 1;
    down.setAttribute("aria-label", t("new_task.file_move_down"));
    down.setAttribute("data-tooltip", t("new_task.file_move_down"));
    down.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>';
    down.addEventListener("click", () => moveStagedFile(index, index + 1));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-btn ghost file-row-remove";
    remove.setAttribute("aria-label", t("new_task.file_remove"));
    remove.setAttribute("data-tooltip", t("new_task.file_remove"));
    remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M6 18 18 6"/></svg>';
    remove.addEventListener("click", () => removeStagedFile(index));

    row.append(num, icon, name, size, up, down, remove);
    fileListEl.appendChild(row);
  });

  if (fileFootTextEl) {
    const total = stagedFiles.reduce((sum, f) => sum + (f.size || 0), 0);
    fileFootTextEl.textContent = t("new_task.file_summary", {
      count: stagedFiles.length,
      size: formatFileSize(total),
    });
  }
  syncFileInput();
}

function addStagedFiles(files) {
  const incoming = Array.from(files || []);
  if (!incoming.length) return;
  const known = new Set(stagedFiles.map(fileKey));
  const duplicates = [];
  for (const file of incoming) {
    const key = fileKey(file);
    if (known.has(key)) {
      duplicates.push(file.name);
      continue;
    }
    known.add(key);
    stagedFiles.push(file);
  }
  // Duplicates are reported rather than dropped silently: picking the same file
  // twice is usually a mistake, and silence looks like the file was lost.
  showFileWarning(
    duplicates.length
      ? t("new_task.file_duplicate", { names: duplicates.join(", "), count: duplicates.length })
      : "",
  );
  renderStagedFiles();
  clearTaskFormError();
}

function removeStagedFile(index) {
  if (index < 0 || index >= stagedFiles.length) return;
  stagedFiles.splice(index, 1);
  showFileWarning("");
  renderStagedFiles();
}

function moveStagedFile(from, to) {
  if (from === to || from < 0 || to < 0 || from >= stagedFiles.length || to >= stagedFiles.length) return;
  const [moved] = stagedFiles.splice(from, 1);
  stagedFiles.splice(to, 0, moved);
  renderStagedFiles();
  // Keep the moved row focused so repeated keyboard moves do not lose the caret.
  const rows = fileListEl?.querySelectorAll(".file-row");
  const target = rows && rows[to];
  target?.querySelector(from < to ? ".file-row-move + .file-row-move" : ".file-row-move")?.focus();
}

function clearStagedFiles() {
  stagedFiles.length = 0;
  showFileWarning("");
  renderStagedFiles();
}

// Drag to reorder. dragover must preventDefault or the drop never fires.
let dragFrom = null;
fileListEl?.addEventListener("dragstart", (e) => {
  const row = e.target instanceof Element ? e.target.closest(".file-row") : null;
  if (!row) return;
  dragFrom = Number(row.dataset.index);
  row.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  // Firefox ignores a drag that sets no data.
  e.dataTransfer.setData("text/plain", String(dragFrom));
});
fileListEl?.addEventListener("dragover", (e) => {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
});
fileListEl?.addEventListener("drop", (e) => {
  e.preventDefault();
  const row = e.target instanceof Element ? e.target.closest(".file-row") : null;
  if (!row || dragFrom === null) return;
  moveStagedFile(dragFrom, Number(row.dataset.index));
  dragFrom = null;
});
fileListEl?.addEventListener("dragend", () => {
  fileListEl.querySelectorAll(".dragging").forEach((el) => el.classList.remove("dragging"));
  dragFrom = null;
});

// The empty state invites a drop, so the zone has to accept one. Without the
// dragover preventDefault the browser just navigates to the dropped file.
fileDropEl?.addEventListener("dragover", (e) => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  fileDropEl.classList.add("drag-over");
});
fileDropEl?.addEventListener("dragleave", (e) => {
  // Ignore the events fired while moving between children of the zone.
  if (e.relatedTarget instanceof Node && fileDropEl.contains(e.relatedTarget)) return;
  fileDropEl.classList.remove("drag-over");
});
fileDropEl?.addEventListener("drop", (e) => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  fileDropEl.classList.remove("drag-over");
  addStagedFiles(e.dataTransfer.files);
});

document.getElementById("file-pick-btn")?.addEventListener("click", () => {
  document.getElementById("file-input")?.click();
});
document.getElementById("file-add-btn")?.addEventListener("click", () => {
  document.getElementById("file-input")?.click();
});

document.getElementById("file-input")?.addEventListener("change", (e) => {
  const input = e.target;
  const picked = Array.from(input.files || []);
  // Ignore the programmatic re-assignment made by syncFileInput().
  if (picked.length === stagedFiles.length && picked.every((f, i) => f === stagedFiles[i])) return;
  addStagedFiles(picked);
});
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

// ---------------------------------------------------------------------------
// UI language: en -> ru -> de -> en.
//
// Until now the interface language came only from navigator.languages, with no
// way to override it — a German-locale browser could not read the app in
// English. The stored choice wins over detection; clearing it (not offered in
// the UI) falls back to the browser.
//
// The label deliberately carries NO data-i18n: it shows the endonym, which is
// the same string in every locale, and applyI18n() would overwrite it.
const LOCALE_CYCLE = ["en", "ru", "de"];
const LOCALE_ENDONYM = { en: "English", ru: "Русский", de: "Deutsch" };

function syncLocaleControl() {
  const label = document.getElementById("locale-toggle-label");
  if (label) label.textContent = LOCALE_ENDONYM[state.locale] || LOCALE_ENDONYM.en;
}

document.getElementById("locale-toggle-btn")?.addEventListener("click", async () => {
  const next = LOCALE_CYCLE[(LOCALE_CYCLE.indexOf(state.locale) + 1) % LOCALE_CYCLE.length];
  const loaded = await loadLocaleScript(next);
  if (!loaded) return;
  state.locale = next;
  try {
    localStorage.setItem("vts_locale", next);
  } catch (err) {
    /* not persisting is survivable; the current page is already switched */
  }
  applyI18nToPage();
  // applyI18nToPage() only touches [data-i18n] nodes. Labels built in JS —
  // the prompt and delivery pills, which read t() at render time — keep the
  // old language until their widget is rebuilt, so rebuild them here.
  // Both take the current selection as an argument, so re-rendering must not
  // (and does not) reset what the user picked.
  repaintJsBuiltLabels();
  // applyI18nToPage() rewrites every [data-i18n] node, which includes the theme
  // label — re-sync it (and the endonym, which it must NOT translate).
  syncThemeControl(readStoredTheme());
  syncLocaleControl();
});

// Widgets whose visible text is produced by t() in JS rather than by a
// data-i18n attribute. applyI18nToPage() cannot reach them, so every locale
// change has to rebuild them explicitly — passing the CURRENT selection so the
// repaint is purely cosmetic.
function repaintJsBuiltLabels() {
  if (promptSelect) {
    renderPromptMultiselect(promptSelect, promptsCache, getSelectedPrompts());
  }
  if (deliverySelect && deliveryTargetsList().length > 0) {
    renderDeliveryMultiselect(deliverySelect, selectedDeliveryRefs(deliverySelect));
  }
  // The segmented filter's labels are copied from the select's options, which
  // applyI18nToPage() has just retranslated — copy them across again.
  renderFilterTypeSegments();
  // Task cards render their status text and step labels through t() too.
  document.querySelectorAll(".task").forEach((card) => {
    if (card._runtime && card._elements) renderTaskRuntime(card);
  });
}

// ---------------------------------------------------------------------------
// Theme: system -> light -> dark -> system.
//
// "System" is the absence of the data-theme attribute, not a third value:
// styles.css themes the dark case through
//   @media (prefers-color-scheme: dark) :root:not([data-theme="light"])
// so no attribute means "follow the OS", and an explicit choice always wins.
// The stored value is therefore only ever "light", "dark", or absent — the
// same contract the inline anti-flash script in index.html reads.
const THEME_CYCLE = ["system", "light", "dark"];
const THEME_LABEL_KEY = { system: "theme.system", light: "theme.light", dark: "theme.dark" };

function readStoredTheme() {
  try {
    const v = localStorage.getItem("vts_theme");
    return v === "light" || v === "dark" ? v : "system";
  } catch (err) {
    // Private mode or storage disabled: behave as if nothing was ever chosen.
    return "system";
  }
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") {
    root.setAttribute("data-theme", mode);
  } else {
    root.removeAttribute("data-theme");
  }
  try {
    if (mode === "system") localStorage.removeItem("vts_theme");
    else localStorage.setItem("vts_theme", mode);
  } catch (err) {
    /* not persisting is survivable; the current page still themes correctly */
  }
  syncThemeControl(mode);
}

// The <meta name="theme-color"> pair in index.html is keyed on the OS
// preference, which cannot express "user chose light while the OS is dark".
// Once a manual choice exists we drop the media-scoped tags and pin a single
// value; going back to system restores the pair.
function syncThemeColorMeta(mode) {
  const head = document.head;
  if (!head) return;
  head.querySelectorAll('meta[name="theme-color"]').forEach((el) => el.remove());
  const add = (content, media) => {
    const m = document.createElement("meta");
    m.setAttribute("name", "theme-color");
    m.setAttribute("content", content);
    if (media) m.setAttribute("media", media);
    head.appendChild(m);
  };
  if (mode === "light") add("#c5532a");
  else if (mode === "dark") add("#191512");
  else {
    add("#c5532a", "(prefers-color-scheme: light)");
    add("#191512", "(prefers-color-scheme: dark)");
  }
}

function syncThemeControl(mode) {
  const label = document.getElementById("theme-toggle-label");
  if (label) {
    label.setAttribute("data-i18n", THEME_LABEL_KEY[mode] || THEME_LABEL_KEY.system);
    label.textContent = t(THEME_LABEL_KEY[mode] || THEME_LABEL_KEY.system);
  }
  for (const name of THEME_CYCLE) {
    document.getElementById(`theme-icon-${name}`)?.classList.toggle("hidden", name !== mode);
  }
  syncThemeColorMeta(mode);
}

document.getElementById("theme-toggle-btn")?.addEventListener("click", () => {
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(readStoredTheme()) + 1) % THEME_CYCLE.length];
  applyTheme(next);
});

// While the choice is "system", a live OS switch must repaint immediately.
// Only the label and meta need touching — the CSS media block already does
// the colours on its own.
window.matchMedia?.("(prefers-color-scheme: dark)")?.addEventListener?.("change", () => {
  if (readStoredTheme() === "system") syncThemeControl("system");
});

syncThemeControl(readStoredTheme());

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

// ---------- Speaker voice registry dialog ----------

const speakerRegistryDialog = document.getElementById("speaker-registry-dialog");
const speakerListEl = document.getElementById("speaker-list");
const speakerSamplesEl = document.getElementById("speaker-samples");
const speakerSamplesEmptyEl = document.getElementById("speaker-samples-empty");
const speakerCreateForm = document.getElementById("speaker-create-form");
const speakerCreateNameInput = document.getElementById("speaker-create-name");

const speakerPickerDialog = document.getElementById("speaker-picker-dialog");
const speakerPickerTitleEl = document.getElementById("speaker-picker-title");
const speakerPickerListEl = document.getElementById("speaker-picker-list");
const speakerPickerEmptyEl = document.getElementById("speaker-picker-empty");
const speakerPickerSortEl = document.getElementById("speaker-picker-sort");
const speakerPickerSortDistanceBtn = document.getElementById("speaker-picker-sort-distance");
const speakerPickerSortAlphaBtn = document.getElementById("speaker-picker-sort-alpha");
const speakerPickerCloseBtn = document.getElementById("speaker-picker-close-btn");
const speakerPickerCancelBtn = document.getElementById("speaker-picker-cancel");

let speakerRegistryCache = [];
let selectedSpeakerId = "";
// Picker state: candidates as loaded, how they are currently ordered, and what
// to do once one is chosen. Shared by "move fragment" and "merge persons".
let speakerPickerCandidates = [];
let speakerPickerSort = "distance";
let speakerPickerOnPick = null;
let speakerPickerAllowNew = false;

function speakerRowById(id) {
  return speakerListEl?.querySelector(`[data-speaker-id="${CSS.escape(String(id))}"]`);
}

function renderSpeakers(list) {
  speakerRegistryCache = Array.isArray(list) ? list : [];
  if (!speakerListEl) return;
  speakerListEl.innerHTML = "";
  if (!speakerRegistryCache.length) {
    const empty = document.createElement("p");
    empty.className = "tokens-empty";
    empty.textContent = t("speakers.registry.empty");
    speakerListEl.appendChild(empty);
    return;
  }
  for (const speaker of speakerRegistryCache) {
    const row = document.createElement("li");
    row.className = "tokens-row speaker-row";
    row.dataset.speakerId = speaker.id;
    if (speaker.id === selectedSpeakerId) row.classList.add("selected");

    const meta = document.createElement("div");
    meta.className = "tokens-meta speaker-meta";

    const nameEl = document.createElement("span");
    nameEl.className = "tokens-name speaker-name";
    nameEl.textContent = speaker.name;
    meta.appendChild(nameEl);

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "speaker-name-input hidden";
    nameInput.maxLength = 255;
    nameInput.value = speaker.name;
    meta.appendChild(nameInput);

    row.appendChild(meta);

    // Row itself selects the speaker; clicking the name/action buttons must
    // not also trigger selection when entering rename mode.
    row.addEventListener("click", (event) => {
      if (row.classList.contains("editing")) return;
      if (event.target.closest(".speaker-actions")) return;
      selectSpeaker(speaker.id);
    });

    const actions = document.createElement("div");
    actions.className = "speaker-actions";

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "icon-btn ghost";
    renameBtn.setAttribute("data-tooltip", t("speakers.registry.rename"));
    renameBtn.setAttribute("aria-label", t("speakers.registry.rename"));
    renameBtn.innerHTML = ICON_EDIT;
    renameBtn.addEventListener("click", () => enterSpeakerRename(row, speaker));
    actions.appendChild(renameBtn);

    // The merge button sits on the SOURCE — the person that will disappear.
    const mergeBtn = document.createElement("button");
    mergeBtn.type = "button";
    mergeBtn.className = "icon-btn ghost speaker-merge-btn";
    mergeBtn.setAttribute("data-tooltip", t("speakers.registry.merge"));
    mergeBtn.setAttribute("aria-label", t("speakers.registry.merge"));
    mergeBtn.innerHTML = ICON_MERGE;
    mergeBtn.addEventListener("click", () => mergeSpeaker(speaker));
    actions.appendChild(mergeBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "icon-btn ghost danger";
    delBtn.setAttribute("data-tooltip", t("speakers.registry.delete"));
    delBtn.setAttribute("aria-label", t("speakers.registry.delete"));
    delBtn.innerHTML = ICON_DELETE;
    delBtn.addEventListener("click", () => deleteSpeaker(speaker));
    actions.appendChild(delBtn);

    row.appendChild(actions);
    speakerListEl.appendChild(row);
  }
}

function enterSpeakerRename(row, speaker) {
  const nameEl = row.querySelector(".speaker-name");
  const nameInput = row.querySelector(".speaker-name-input");
  if (!nameEl || !nameInput) return;
  row.classList.add("editing");
  nameEl.classList.add("hidden");
  nameInput.classList.remove("hidden");
  nameInput.value = speaker.name;
  nameInput.focus();
  nameInput.select();

  const commit = async () => {
    nameInput.removeEventListener("keydown", onKeydown);
    nameInput.removeEventListener("blur", commit);
    const value = nameInput.value.trim();
    if (!value || value === speaker.name) {
      cancel();
      return;
    }
    nameInput.disabled = true;
    try {
      await api(`/api/speakers/${encodeURIComponent(speaker.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: value }),
      });
      await refreshSpeakerRegistry();
    } catch (err) {
      console.error("speaker rename failed", err);
      cancel();
    }
  };

  const cancel = () => {
    nameInput.removeEventListener("keydown", onKeydown);
    nameInput.removeEventListener("blur", commit);
    row.classList.remove("editing");
    nameEl.classList.remove("hidden");
    nameInput.classList.add("hidden");
  };

  function onKeydown(event) {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  }

  nameInput.addEventListener("keydown", onKeydown);
  nameInput.addEventListener("blur", commit);
}

async function deleteSpeaker(speaker) {
  const count = Number(speaker.sample_count) || 0;
  const confirmed = window.confirm(
    t("speakers.registry.delete_confirm", { name: speaker.name, count })
  );
  if (!confirmed) return;
  try {
    await api(`/api/speakers/${encodeURIComponent(speaker.id)}`, { method: "DELETE" });
  } catch (err) {
    console.error("speaker delete failed", err);
    return;
  }
  if (selectedSpeakerId === speaker.id) {
    selectedSpeakerId = "";
    renderSamples([]);
  }
  await refreshSpeakerRegistry();
}

async function selectSpeaker(speakerId) {
  selectedSpeakerId = speakerId;
  speakerListEl?.querySelectorAll(".speaker-row").forEach((row) => {
    row.classList.toggle("selected", row.dataset.speakerId === speakerId);
  });
  await refreshSpeakerSamples(speakerId);
}

function renderSamples(samples) {
  if (!speakerSamplesEl) return;
  const list = Array.isArray(samples) ? samples : [];
  speakerSamplesEl.innerHTML = "";
  const hasSelection = !!selectedSpeakerId;
  speakerSamplesEmptyEl?.classList.toggle("hidden", hasSelection);
  speakerSamplesEl.classList.toggle("hidden", !hasSelection);
  if (!hasSelection) return;

  if (!list.length) {
    const empty = document.createElement("p");
    empty.className = "tokens-empty";
    empty.textContent = t("speakers.registry.samples_empty");
    speakerSamplesEl.appendChild(empty);
    return;
  }

  for (const sample of list) {
    const row = document.createElement("li");
    row.className = "tokens-row speaker-sample-row";

    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = buildPath(`/api/speakers/samples/${encodeURIComponent(sample.id)}/audio`);
    row.appendChild(audio);

    const meta = document.createElement("div");
    meta.className = "tokens-meta speaker-sample-meta";

    const duration = document.createElement("span");
    duration.className = "speaker-sample-duration";
    duration.textContent = formatDuration(sample.duration_sec || 0);
    meta.appendChild(duration);

    const created = document.createElement("span");
    created.className = "speaker-sample-created";
    created.textContent = sample.created_at ? new Date(sample.created_at).toLocaleString() : "";
    meta.appendChild(created);

    const source = document.createElement("span");
    source.className = "speaker-sample-source";
    if (sample.source_task_id) {
      const link = document.createElement("a");
      link.href = "#";
      link.className = "speaker-sample-source-link";
      link.textContent = t("speakers.registry.from_task");
      link.addEventListener("click", (event) => {
        event.preventDefault();
        jumpToTask(sample.source_task_id);
      });
      source.appendChild(link);
    } else {
      source.textContent = t("speakers.registry.from_task_gone");
    }
    meta.appendChild(source);

    row.appendChild(meta);

    const moveBtn = document.createElement("button");
    moveBtn.type = "button";
    moveBtn.className = "icon-btn ghost speaker-sample-move-btn";
    moveBtn.setAttribute("data-tooltip", t("speakers.registry.move_sample"));
    moveBtn.setAttribute("aria-label", t("speakers.registry.move_sample"));
    moveBtn.innerHTML = ICON_MOVE;
    moveBtn.addEventListener("click", () => moveSample(sample));
    row.appendChild(moveBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "icon-btn ghost danger";
    delBtn.setAttribute("data-tooltip", t("speakers.registry.delete_sample"));
    delBtn.setAttribute("aria-label", t("speakers.registry.delete_sample"));
    delBtn.innerHTML = ICON_DELETE;
    delBtn.addEventListener("click", () => deleteSample(sample));
    row.appendChild(delBtn);

    speakerSamplesEl.appendChild(row);
  }
}

async function deleteSample(sample) {
  const confirmed = window.confirm(t("speakers.registry.delete_sample_confirm"));
  if (!confirmed) return;
  try {
    await api(
      `/api/speakers/${encodeURIComponent(selectedSpeakerId)}/samples/${encodeURIComponent(sample.id)}`,
      { method: "DELETE" }
    );
  } catch (err) {
    console.error("speaker sample delete failed", err);
    return;
  }
  await refreshSpeakerSamples(selectedSpeakerId);
  await refreshSpeakerRegistry({ keepSamples: true });
}

// ---------- Person picker (move fragment / merge persons) ----------

function closeSpeakerPicker() {
  speakerPickerOnPick = null;
  speakerPickerCandidates = [];
  speakerPickerDialog?.close();
}

function sortedPickerCandidates() {
  const list = speakerPickerCandidates.slice();
  if (speakerPickerSort === "alpha") {
    list.sort((a, b) => String(a.name).localeCompare(String(b.name)));
    return list;
  }
  // Distance order: the server already ranked these, but a candidate with no
  // comparable fragment carries distance null and must sink to the bottom
  // rather than sorting as if it were nearest.
  list.sort((a, b) => {
    const da = a.distance === null || a.distance === undefined ? Infinity : Number(a.distance);
    const db = b.distance === null || b.distance === undefined ? Infinity : Number(b.distance);
    if (da === db) return String(a.name).localeCompare(String(b.name));
    return da - db;
  });
  return list;
}

function renderSpeakerPicker() {
  if (!speakerPickerListEl) return;
  speakerPickerListEl.innerHTML = "";

  const rows = [];
  if (speakerPickerAllowNew) {
    // "Create new" stays first in BOTH orderings — it is an action, not a
    // candidate, so it must not drift into the middle of the sorted list.
    rows.push({ id: "", name: t("speakers.registry.move_new_person"), isNew: true });
  }
  rows.push(...sortedPickerCandidates());

  const hasCandidates = speakerPickerCandidates.length > 0;
  speakerPickerEmptyEl?.classList.toggle("hidden", hasCandidates);
  speakerPickerSortEl?.classList.toggle("hidden", !hasCandidates);

  for (const item of rows) {
    const row = document.createElement("li");
    row.className = "tokens-row speaker-picker-row";
    if (item.isNew) row.classList.add("speaker-picker-new");
    row.dataset.speakerId = item.id || "";

    const meta = document.createElement("div");
    meta.className = "tokens-meta";

    const nameEl = document.createElement("span");
    nameEl.className = "tokens-name";
    nameEl.textContent = item.name;
    meta.appendChild(nameEl);

    if (!item.isNew) {
      const dist = document.createElement("span");
      dist.className = "speaker-picker-distance";
      dist.textContent =
        item.distance === null || item.distance === undefined
          ? t("speakers.registry.no_distance")
          : Number(item.distance).toFixed(3);
      meta.appendChild(dist);
    }

    row.appendChild(meta);
    row.addEventListener("click", () => {
      const handler = speakerPickerOnPick;
      if (handler) handler(item);
    });
    speakerPickerListEl.appendChild(row);
  }
}

function openSpeakerPicker({ title, candidates, allowNew, emptyText, onPick }) {
  speakerPickerCandidates = Array.isArray(candidates) ? candidates : [];
  speakerPickerAllowNew = !!allowNew;
  speakerPickerOnPick = onPick;
  speakerPickerSort = "distance";
  if (speakerPickerTitleEl) speakerPickerTitleEl.textContent = title;
  if (speakerPickerEmptyEl) speakerPickerEmptyEl.textContent = emptyText || "";
  speakerPickerSortDistanceBtn?.classList.add("active");
  speakerPickerSortAlphaBtn?.classList.remove("active");
  renderSpeakerPicker();
  if (speakerPickerDialog && !speakerPickerDialog.open) speakerPickerDialog.showModal();
}

speakerPickerSortDistanceBtn?.addEventListener("click", () => {
  speakerPickerSort = "distance";
  speakerPickerSortDistanceBtn.classList.add("active");
  speakerPickerSortAlphaBtn?.classList.remove("active");
  renderSpeakerPicker();
});

speakerPickerSortAlphaBtn?.addEventListener("click", () => {
  speakerPickerSort = "alpha";
  speakerPickerSortAlphaBtn.classList.add("active");
  speakerPickerSortDistanceBtn?.classList.remove("active");
  renderSpeakerPicker();
});

speakerPickerCloseBtn?.addEventListener("click", closeSpeakerPicker);
speakerPickerCancelBtn?.addEventListener("click", closeSpeakerPicker);

async function moveSample(sample) {
  let candidates = [];
  try {
    candidates = await api(
      `/api/speakers/${encodeURIComponent(selectedSpeakerId)}/samples/${encodeURIComponent(sample.id)}/move-candidates`
    );
  } catch (err) {
    console.error("Failed to load move candidates", err);
    return;
  }

  openSpeakerPicker({
    title: t("speakers.registry.move_title"),
    candidates,
    allowNew: true,
    emptyText: t("speakers.registry.move_empty"),
    onPick: async (item) => {
      let targetId = item.id;
      let targetName = item.name;

      if (item.isNew) {
        const name = window.prompt(t("speakers.registry.move_new_name_prompt"));
        if (!name || !name.trim()) return;
        try {
          const created = await api("/api/speakers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim() }),
          });
          targetId = created.id;
          targetName = created.name;
        } catch (err) {
          console.error("speaker create failed", err);
          return;
        }
      } else if (!window.confirm(t("speakers.registry.move_confirm", { name: targetName }))) {
        return;
      }

      try {
        await api(
          `/api/speakers/${encodeURIComponent(selectedSpeakerId)}/samples/${encodeURIComponent(sample.id)}/move`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target_speaker_id: targetId }),
          }
        );
      } catch (err) {
        console.error("sample move failed", err);
        return;
      }
      closeSpeakerPicker();
      await refreshSpeakerRegistry();
    },
  });
}

async function mergeSpeaker(source) {
  const others = speakerRegistryCache.filter((s) => s.id !== source.id);
  openSpeakerPicker({
    title: t("speakers.registry.merge_title", { name: source.name }),
    // Merge targets an EXISTING person: "create new" makes no sense here,
    // and distance ranking needs a fragment, which a person-level merge has not.
    candidates: others.map((s) => ({ id: s.id, name: s.name, distance: null })),
    allowNew: false,
    emptyText: t("speakers.registry.merge_empty"),
    onPick: async (item) => {
      const confirmed = window.confirm(
        t("speakers.registry.merge_confirm", { source: source.name, target: item.name })
      );
      if (!confirmed) return;
      try {
        await api(`/api/speakers/${encodeURIComponent(source.id)}/merge`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_id: item.id }),
        });
      } catch (err) {
        console.error("speaker merge failed", err);
        return;
      }
      closeSpeakerPicker();
      // The source is gone; if it was selected, follow the data to the target.
      if (selectedSpeakerId === source.id) {
        selectedSpeakerId = item.id;
      }
      await refreshSpeakerRegistry();
      if (selectedSpeakerId) await refreshSpeakerSamples(selectedSpeakerId);
    },
  });
}

function jumpToTask(taskId) {
  const row = findTaskEl(taskId);
  if (!row) return;
  speakerRegistryDialog?.close();
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 2000);
}

async function refreshSpeakerSamples(speakerId) {
  if (!speakerId) {
    renderSamples([]);
    return;
  }
  try {
    const samples = await api(`/api/speakers/${encodeURIComponent(speakerId)}/samples`);
    renderSamples(samples);
  } catch (err) {
    console.error("Failed to load speaker samples", err);
    renderSamples([]);
  }
}

async function refreshSpeakerRegistry(options = {}) {
  if (!speakerListEl) return;
  try {
    const speakers = await api("/api/speakers");
    renderSpeakers(speakers);
    if (selectedSpeakerId && !speakers.some((s) => s.id === selectedSpeakerId)) {
      selectedSpeakerId = "";
      renderSamples([]);
    } else if (selectedSpeakerId && !options.keepSamples) {
      await refreshSpeakerSamples(selectedSpeakerId);
    }
  } catch (err) {
    console.error("Failed to load speakers", err);
  }
}

async function openSpeakerRegistry() {
  if (!speakerRegistryDialog) return;
  selectedSpeakerId = "";
  if (speakerCreateNameInput) speakerCreateNameInput.value = "";
  renderSamples([]);
  await refreshSpeakerRegistry();
  if (typeof speakerRegistryDialog.showModal === "function") {
    speakerRegistryDialog.showModal();
  } else {
    speakerRegistryDialog.setAttribute("open", "");
  }
}

document.getElementById("speaker-registry-btn")?.addEventListener("click", () => {
  openSpeakerRegistry();
});

document.getElementById("speaker-registry-close-btn")?.addEventListener("click", () => {
  speakerRegistryDialog?.close();
});

speakerCreateForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = (speakerCreateNameInput?.value || "").trim();
  if (!name) return;
  try {
    const speaker = await api("/api/speakers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (speakerCreateNameInput) speakerCreateNameInput.value = "";
    await refreshSpeakerRegistry({ keepSamples: true });
    if (speaker?.id) await selectSpeaker(speaker.id);
  } catch (err) {
    console.error("Failed to create speaker", err);
  }
});

// ---------- Voice-resolution dialog (vts-80i, task 14) ----------
//
// Opened by the "Доработать" button on an awaiting_input task. Fetches
// GET /api/tasks/{id}/speaker-matches (speaker_matches.json) and GET
// /api/speakers (ALL of the user's speakers, for the dropdown — not just the
// matched candidates, per spec: truncating to top-N would push a user whose
// person isn't in the first N into creating a duplicate, feeding vts-552).
// One row per speaker_label: a status glyph, a preview <audio> (best-effort —
// no task-preview-audio route exists yet, see report), and ONE <select> that
// lists every speaker sorted by distance plus "<Add new person>". Saving
// POSTs all resolutions in one transaction via POST /api/tasks/{id}/speakers.
//
// No task preview-audio route exists (checked against vts/api/main.py): the
// brief explicitly allows shipping without it rather than blocking, so the
// preview <audio> renders with no src and a "preview unavailable" label.

const voiceDialog = document.getElementById("voice-resolution-dialog");
const voiceListEl = document.getElementById("voice-list");
const voiceListEmptyEl = document.getElementById("voice-list-empty");
const voiceSaveBtn = document.getElementById("voice-save");
const voiceSaveContinueBtn = document.getElementById("voice-save-continue");
const voiceCancelBtn = document.getElementById("voice-cancel");

const NEW_PERSON_VALUE = "__new__";

let voiceDialogState = null; // { taskId, rows: [...], dirty }

// One row's mutable UI state, seeded from speaker_matches.json.
// ---------------------------------------------------------------------------
// Speakers panel in the task card (redesign v2).
//
// The bindings existed only inside the voice-resolution dialog, so who is in a
// recording — and who they were bound to — was invisible until you went looking
// for it. The panel shows that in the card, and each row is a way into the
// existing speaker picker rather than a second implementation of it.
//
// Loaded lazily on first expand: /speaker-matches is a real request, and most
// cards are never expanded.

async function loadSpeakerPanel(taskEl, taskId) {
  const box = taskEl.querySelector(".speaker-box");
  if (!box || taskEl._speakerPanelLoading) return;
  taskEl._speakerPanelLoading = true;
  try {
    const [matches, speakers] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(taskId)}/speaker-matches`),
      api("/api/speakers"),
    ]);
    const allSpeakers = Array.isArray(speakers) ? speakers : [];
    const labels = Object.keys(matches || {}).sort();
    // Reuse buildVoiceRow: the "which person is this bound to" logic is subtle
    // (a saved decision outranks the auto-match, an anonymous decision stands)
    // and duplicating it here would let the panel and the dialog disagree.
    taskEl._speakerRows = labels
      .map((label) => buildVoiceRow(label, matches[label] || {}, allSpeakers))
      .sort((a, b) => b.share - a.share);
    renderSpeakerPanel(taskEl, taskId);
  } catch (err) {
    console.error("Failed to load speakers for the task panel", err);
    // Leave the panel hidden rather than showing an empty shell: a card with no
    // diarization legitimately has nothing here.
  } finally {
    taskEl._speakerPanelLoading = false;
  }
}

// One <audio> per card, created on first use. Stopping the previous clip before
// starting a new one is the whole point: comparing two voices means hearing them
// in turn, not at once.
function playSpeakerPreview(taskEl, taskId, row, btn) {
  if (!taskEl._speakerAudio) {
    taskEl._speakerAudio = new Audio();
    taskEl._speakerAudio.preload = "none";
  }
  const audio = taskEl._speakerAudio;
  const src = buildPath(
    `/api/tasks/${encodeURIComponent(taskId)}/speaker-previews/${encodeURIComponent(row.label)}/0/audio`
  );
  const playingThis = taskEl._speakerAudioLabel === row.label && !audio.paused;
  taskEl.querySelectorAll(".avatar-play.playing").forEach((el) => el.classList.remove("playing"));
  if (playingThis) {
    audio.pause();
    taskEl._speakerAudioLabel = null;
    return;
  }
  // buildPath, not a bare URL: an admin viewing another user's task has
  // state.actingAs set, and the endpoint needs that param to authorize the
  // fetch — without it the request 404s (vts-552).
  audio.src = src;
  taskEl._speakerAudioLabel = row.label;
  btn.classList.add("playing");
  audio.onended = () => btn.classList.remove("playing");
  audio.onerror = () => {
    btn.classList.remove("playing");
    btn.classList.add("no-preview");
    btn.setAttribute("data-tooltip", t("voices.row.preview_unavailable"));
  };
  void audio.play().catch(() => {
    btn.classList.remove("playing");
  });
}

function speakerInitials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return parts.slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

function renderSpeakerPanel(taskEl, taskId) {
  const box = taskEl.querySelector(".speaker-box");
  const list = box?.querySelector(".speaker-box-list");
  const countEl = box?.querySelector(".speaker-box-count");
  const rows = Array.isArray(taskEl._speakerRows) ? taskEl._speakerRows : [];
  if (!box || !list) return;

  // Shown whenever diarization produced speakers — not only while the task is
  // waiting on input (Victor's call).
  box.classList.toggle("hidden", rows.length === 0);
  if (!rows.length) return;
  if (countEl) countEl.textContent = t("speakers.panel.count", { count: rows.length });

  list.textContent = "";
  for (const row of rows) {
    // "Bound" is narrower than "preselected": buildVoiceRow pre-fills the dialog
    // with the nearest candidate even for a grey match nobody confirmed. Only a
    // saved operator decision or a committed auto-match counts here, otherwise
    // the panel claims a binding the backend never made — and hides the chips
    // that exist to make it.
    const decided = row.decidedSpeakerId || (row.outcome === "auto" ? row.matchedSpeakerId : null);
    const boundOption = decided ? row.options.find((o) => o.speaker_id === decided) : null;
    const bound = Boolean(boundOption);

    const item = document.createElement("div");
    item.className = "spk-panel-row";
    if (!bound) item.classList.add("unbound");

    // Play the voice's preview clip. One shared <audio> per card rather than one
    // per row: several players would let two clips overlap, and the point is to
    // compare voices one at a time.
    const play = document.createElement("button");
    play.type = "button";
    play.className = "avatar avatar-play";
    play.setAttribute("aria-label", t("speakers.panel.play"));
    play.setAttribute("data-tooltip", t("speakers.panel.play"));
    play.textContent = bound ? speakerInitials(boundOption.name) : "?";
    play.addEventListener("click", () => playSpeakerPreview(taskEl, taskId, row, play));
    const avatar = play;

    const label = document.createElement("span");
    label.className = "spk-panel-label";
    label.textContent = row.displayLabel;

    const share = document.createElement("span");
    share.className = "spk-panel-share mono";
    share.textContent = `${Math.round((row.share || 0) * 100)}%`;

    const action = document.createElement("button");
    action.type = "button";
    action.className = bound ? "btn-link spk-panel-person" : "btn-link spk-panel-pick";
    action.textContent = bound ? boundOption.name : t("speakers.panel.pick");
    action.addEventListener("click", () => openSpeakerPanelPicker(taskEl, taskId, row));

    const noise = document.createElement("button");
    noise.type = "button";
    noise.className = "spk-panel-noise" + (row.noise ? " is-on" : "");
    noise.setAttribute("aria-pressed", String(Boolean(row.noise)));
    noise.setAttribute("aria-label", t("speakers.panel.noise"));
    noise.setAttribute("data-tooltip", t("speakers.panel.noise"));
    noise.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10v4h4l5 4V6L7 10H3z"/><path d="m16 9 5 6M21 9l-5 6"/></svg>';
    noise.addEventListener("click", async () => {
      // A voice marked as noise is not a person, so binding it makes no sense —
      // the backend records the flag with the resolution set.
      row.noise = !row.noise;
      await bindSpeakerRow(taskEl, taskId, row, row.selection === NEW_PERSON_VALUE ? null : row.selection);
    });
    if (row.noise) item.classList.add("is-noise");

    item.append(avatar, label, share, noise, action);

    // "Looks like" chips: only for voices nobody is bound to yet. Anything within
    // speaker_match_max_distance_auto (0.25) was auto-bound already, so showing
    // candidates there would restate a decision instead of helping make one.
    if (!bound) {
      const near = row.options
        .filter((o) => typeof o.distance === "number" && o.distance <= SPEAKER_CANDIDATE_MAX_DISTANCE)
        .slice(0, 3);
      if (near.length) {
        const hints = document.createElement("div");
        hints.className = "spk-panel-hints";
        const caption = document.createElement("span");
        caption.className = "spk-panel-hints-label";
        caption.textContent = t("speakers.panel.looks_like");
        hints.appendChild(caption);
        for (const cand of near) {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "speaker-chip";
          const chipName = document.createElement("span");
          chipName.textContent = cand.name;
          const chipPct = document.createElement("span");
          chipPct.className = "speaker-chip-pct";
          // Cosine distance -> a similarity the user can read. The scale is 0..1
          // (see speaker_match_max_distance_* in vts/core/config.py), so 1 - d is
          // honest rather than a made-up curve.
          chipPct.textContent = `${Math.round((1 - cand.distance) * 100)}%`;
          chip.append(chipName, chipPct);
          chip.addEventListener("click", () => bindSpeakerRow(taskEl, taskId, row, cand.speaker_id));
          hints.appendChild(chip);
        }
        item.appendChild(hints);
      }
    }

    list.appendChild(item);
  }
}

// One row -> the existing picker. allowNew is false here: creating a person
// mid-list needs the naming flow the registry dialog owns, and the panel's job
// is binding to someone who already exists.
// Mirrors speaker_match_max_distance_candidate in vts/core/config.py: past this
// the matcher does not treat it as a candidate at all, so neither should we.
const SPEAKER_CANDIDATE_MAX_DISTANCE = 0.55;

// Shared by the picker dialog and the similarity chips: both end in the same
// whole-set resubmit, and two copies of that payload would drift apart.
async function bindSpeakerRow(taskEl, taskId, row, speakerId) {
  const previous = row.selection;
  row.selection = speakerId || NEW_PERSON_VALUE;
  const rows = Array.isArray(taskEl._speakerRows) ? taskEl._speakerRows : [row];
  const resolutions = rows.map((r) => {
    const chosen = r.options.find((o) => o.speaker_id === r.selection);
    const base = {
      speaker_label: r.label,
      outcome: r.outcome,
      distance: chosen && typeof chosen.distance === "number" ? chosen.distance : r.matchedDistance,
      is_noise: r.noise,
    };
    if (r.selection === NEW_PERSON_VALUE) {
      return { ...base, action: "leave_anonymous", add_fragment: false };
    }
    if (r.outcome === "auto" && r.selection === r.matchedSpeakerId) {
      return { ...base, action: "accept_auto", speaker_id: r.selection, add_fragment: false };
    }
    return { ...base, action: "bind_existing", speaker_id: r.selection, add_fragment: false };
  });
  // A task parked in awaiting_input is waiting for exactly this: once no voice
  // is left undecided, binding the last one should let it carry on rather than
  // making the user find the dialog just to press Continue. A voice marked as
  // noise counts as decided — it is not a person and never will be.
  const stillUndecided = rows.some(
    (r) => !r.noise && r.selection === NEW_PERSON_VALUE && !r.hasDecision,
  );
  const awaiting = taskEl._runtime?.baseStatus === "awaiting_input";
  const continueTask = awaiting && !stillUndecided;

  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/speakers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolutions, continue_task: continueTask }),
    });
  } catch (err) {
    console.error("Failed to bind the speaker", err);
    row.selection = previous;
    return;
  }
  await loadSpeakerPanel(taskEl, taskId);
  if (continueTask) {
    // Same in-place refresh the dialog uses: loadTasks() would rebuild the list
    // and collapse the card the user is working in (bug #3, vts-552).
    await refreshTaskInPlace(taskId);
  }
}

function openSpeakerPanelPicker(taskEl, taskId, row) {
  const candidates = row.options
    .filter((o) => o.speaker_id !== NEW_PERSON_VALUE)
    .map((o) => ({ id: o.speaker_id, name: o.name, distance: o.distance }));
  openSpeakerPicker({
    title: t("speakers.panel.pick_title", { label: row.displayLabel }),
    candidates,
    allowNew: false,
    emptyText: t("speakers.panel.pick_empty"),
    onPick: async (item) => {
      closeSpeakerPicker();
      await bindSpeakerRow(taskEl, taskId, row, item.id);
    },
  });
}

function buildVoiceRow(label, match, allSpeakers) {
  const outcome = match.outcome === "auto" || match.outcome === "grey" || match.outcome === "miss"
    ? match.outcome
    : "miss";
  const candidates = Array.isArray(match.candidates) ? match.candidates : [];
  const candidateIds = new Set(candidates.map((c) => String(c.speaker_id)));
  // Sort ALL speakers by distance: matched candidates first (already ranked
  // by the matcher), then speakers absent from candidates (no comparable
  // distance — matching had zero fragments to compare, not "infinitely far"),
  // by name so the tail is at least stable/scannable.
  const unmatched = allSpeakers
    .filter((sp) => !candidateIds.has(String(sp.id)))
    .slice()
    .sort((a, b) => String(a.name).localeCompare(String(b.name)));
  const ranked = candidates
    .slice()
    .sort((a, b) => (Number(a.distance) || 0) - (Number(b.distance) || 0))
    .map((c) => ({ speaker_id: String(c.speaker_id), name: c.name, distance: c.distance }));
  const options = ranked.concat(
    unmatched.map((sp) => ({ speaker_id: String(sp.id), name: sp.name, distance: null }))
  );
  // A saved decision wins over the auto-match: reopening the dialog shows what
  // the operator bound (bug #1, vts-552). decided_speaker_id is the person they
  // chose; if it's still a selectable option, preselect it. A decision that
  // left the label anonymous (decided_speaker_id null but a decision exists) is
  // NOT auto-preselected to a candidate — the operator's "anonymous" stands, so
  // it falls through to "add new". Otherwise fall back to the auto-match rule.
  const decidedSpeakerId = match.decided_speaker_id ? String(match.decided_speaker_id) : null;
  const hasDecision = match.decided_is_noise !== null && match.decided_is_noise !== undefined;
  const decidedIsSelectable = decidedSpeakerId && options.some((o) => o.speaker_id === decidedSpeakerId);
  let initialSelection;
  if (decidedIsSelectable) {
    initialSelection = decidedSpeakerId;
  } else if (hasDecision) {
    // Decided anonymous (or the bound person was deleted) -> leave as "add new".
    initialSelection = NEW_PERSON_VALUE;
  } else {
    initialSelection = outcome !== "miss" && options.length > 0
      ? options[0].speaker_id
      : NEW_PERSON_VALUE;
  }
  return {
    label,
    outcome,
    // The transcript-consistent "Голос N" / name label shown to the operator,
    // instead of the raw SPEAKER_NN tag (bug #2, vts-552). Falls back to the
    // technical tag if the backend didn't supply one.
    displayLabel: typeof match.display_label === "string" && match.display_label
      ? match.display_label
      : label,
    matchedSpeakerId: match.speaker_id ? String(match.speaker_id) : null,
    // Exposed for the card's speakers panel, which must tell an operator's saved
    // decision apart from the dialog's pre-fill (see the `bound` check there).
    decidedSpeakerId,
    hasDecision,
    matchedDistance: typeof match.distance === "number" ? match.distance : null,
    options,
    selection: initialSelection,
    initialSelection,
    newName: "",
    addFragment: outcome !== "miss", // default ON for grey/auto; irrelevant (hidden) for miss's initial "add new"
    // Set once the user actually changes the dropdown away from a bound
    // candidate that had a fragment saved by THIS task in a prior save of
    // this same dialog session — drives the rollback confirm. Real backend
    // rollback keys off source_task_id; the UI only needs to know whether
    // the previous save's resolution for this label bound a candidate and
    // added a fragment, tracked via savedBinding below.
    savedBinding: null, // { speaker_id, addedFragment: bool } after a "Save" for this label
    // Noise flag (vts-552): the operator's saved decision wins over the
    // matcher's auto-detection when a decision exists (bug #1), so a reopened
    // dialog reflects what they chose. noiseAuto still records the MATCHER's
    // suggestion (drives the "auto" hint); noiseInitial is the dirty baseline;
    // noise is the live value.
    noise: hasDecision ? Boolean(match.decided_is_noise) : Boolean(match.noise),
    noiseInitial: hasDecision ? Boolean(match.decided_is_noise) : Boolean(match.noise),
    noiseAuto: Boolean(match.noise),
    share: typeof match.share === "number" ? match.share : 0,
    seconds: typeof match.seconds === "number" ? match.seconds : 0,
  };
}

function isVoiceRowDirty(row) {
  if (row.selection !== row.initialSelection) return true;
  if (row.selection === NEW_PERSON_VALUE && row.newName.trim()) return true;
  const defaultAddFragment = row.outcome !== "miss";
  if (row.selection !== NEW_PERSON_VALUE && row.addFragment !== defaultAddFragment) return true;
  if (row.noise !== row.noiseInitial) return true;
  return false;
}

function isVoiceDialogDirty() {
  if (!voiceDialogState) return false;
  return voiceDialogState.rows.some(isVoiceRowDirty);
}

function glyphForOutcome(outcome) {
  if (outcome === "auto") return "🟢";
  if (outcome === "grey") return "🟡";
  return "🔴";
}

function renderVoiceList() {
  if (!voiceListEl || !voiceDialogState) return;
  voiceListEl.innerHTML = "";
  const rows = voiceDialogState.rows;
  voiceListEmptyEl?.classList.toggle("hidden", rows.length > 0);
  voiceListEl.classList.toggle("hidden", rows.length === 0);

  rows.forEach((row) => {
    const li = document.createElement("li");
    li.className = "tokens-row voice-row";
    li.dataset.speakerLabel = row.label;

    const glyph = document.createElement("span");
    glyph.className = "voice-glyph";
    glyph.textContent = glyphForOutcome(row.outcome);
    glyph.title = t(`voices.status.${row.outcome}`);
    glyph.setAttribute("aria-label", t(`voices.status.${row.outcome}`));
    li.appendChild(glyph);

    const body = document.createElement("div");
    body.className = "voice-row-body";

    const labelEl = document.createElement("div");
    labelEl.className = "voice-row-label";
    // Show the transcript-consistent "Голос N" / name label, not the raw
    // technical SPEAKER_NN tag (bug #2, vts-552).
    labelEl.textContent = row.displayLabel;
    body.appendChild(labelEl);

    const audio = document.createElement("audio");
    audio.className = "voice-preview-audio";
    audio.controls = true;
    audio.preload = "none";
    // buildPath, not a bare URL: an admin viewing another user's task has
    // state.actingAs set, and buildPath carries the as_user param the endpoint
    // needs to authorize the fetch. Without it the request resolves to the
    // admin's own (nonexistent) task and 404s, so every preview showed
    // "preview unavailable" with a 0:00 player — exactly the sibling
    // voice-sample player's contract at buildPath(/api/speakers/...) (vts-552).
    audio.src = buildPath(`/api/tasks/${encodeURIComponent(voiceDialogState.taskId)}/speaker-previews/${encodeURIComponent(row.label)}/0/audio`);
    const previewNote = document.createElement("span");
    previewNote.className = "voice-preview-unavailable hidden";
    previewNote.textContent = t("voices.row.preview_unavailable");
    // Graceful fallback: if this row has no preview clip (or the file is
    // otherwise unreachable) the request 404s harmlessly - swap the player
    // for the "unavailable" note instead of leaving a broken control.
    audio.addEventListener("error", () => {
      audio.classList.add("hidden");
      previewNote.classList.remove("hidden");
    });
    body.appendChild(audio);
    body.appendChild(previewNote);

    const select = document.createElement("select");
    select.className = "voice-select";
    const addNewOption = () => {
      const opt = document.createElement("option");
      opt.value = NEW_PERSON_VALUE;
      opt.textContent = t("voices.row.new_person");
      return opt;
    };
    // miss: "<Add new person>" at the TOP (model missed; person list follows
    // in case the user recognizes the voice by ear anyway).
    if (row.outcome === "miss") {
      select.appendChild(addNewOption());
    }
    row.options.forEach((opt) => {
      const el = document.createElement("option");
      el.value = opt.speaker_id;
      el.textContent = opt.name;
      select.appendChild(el);
    });
    // grey/auto: "<Add new person>" at the BOTTOM.
    if (row.outcome !== "miss") {
      select.appendChild(addNewOption());
    }
    select.value = row.selection;
    body.appendChild(select);

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "voice-new-name";
    nameInput.maxLength = 255;
    nameInput.placeholder = t("voices.row.name_placeholder");
    nameInput.value = row.newName;
    nameInput.classList.toggle("hidden", row.selection !== NEW_PERSON_VALUE);
    body.appendChild(nameInput);

    const fragmentLabel = document.createElement("label");
    fragmentLabel.className = "voice-add-fragment";
    const fragmentCheckbox = document.createElement("input");
    fragmentCheckbox.type = "checkbox";
    fragmentCheckbox.checked = row.addFragment;
    fragmentLabel.appendChild(fragmentCheckbox);
    fragmentLabel.appendChild(document.createTextNode(t("voices.row.add_fragment")));
    // Only meaningful when binding to an existing candidate (grey/auto path);
    // hidden while "add new" is selected (fragment is implied there — a
    // brand-new person's first fragment isn't optional the way an addition
    // to an existing person's registry is).
    fragmentLabel.classList.toggle("hidden", row.selection === NEW_PERSON_VALUE);
    body.appendChild(fragmentLabel);

    select.addEventListener("change", () => {
      const previousSelection = row.selection;
      onVoiceRowRebind(row, select.value, previousSelection);
      nameInput.classList.toggle("hidden", row.selection !== NEW_PERSON_VALUE);
      fragmentLabel.classList.toggle("hidden", row.selection === NEW_PERSON_VALUE);
      if (row.selection === NEW_PERSON_VALUE) {
        nameInput.focus();
      }
    });
    nameInput.addEventListener("input", () => {
      row.newName = nameInput.value;
    });
    fragmentCheckbox.addEventListener("change", () => {
      row.addFragment = fragmentCheckbox.checked;
    });

    // Share display (vts-552): "13% · 2:05". row.seconds is the speaker's REAL
    // diarized speaking time from speaker_matches.json — not share * media
    // length, which over-states it because media includes silence. Falls back
    // to percent alone when seconds are unavailable (older tasks).
    const shareEl = document.createElement("span");
    shareEl.className = "voice-row-share";
    const percent = Math.round(row.share * 100);
    shareEl.textContent = row.seconds > 0
      ? t("voices.row.share", { percent, duration: formatDuration(row.seconds) })
      : t("voices.row.share_percent_only", { percent });
    body.appendChild(shareEl);

    // Noise checkbox (vts-552): pre-filled from the matcher; checked -> the row
    // is dimmed (voice-row-noise) and the resolution carries is_noise=true.
    const noiseWrap = document.createElement("label");
    noiseWrap.className = "voice-row-noise-toggle";
    const noiseBox = document.createElement("input");
    noiseBox.type = "checkbox";
    noiseBox.checked = row.noise;
    noiseBox.addEventListener("change", () => {
      row.noise = noiseBox.checked;
      li.classList.toggle("voice-row-noise", row.noise);
    });
    const noiseText = document.createElement("span");
    noiseText.textContent = t("voices.row.noise");
    noiseWrap.append(noiseBox, noiseText);
    body.appendChild(noiseWrap);
    li.classList.toggle("voice-row-noise", row.noise);

    // Auto-detected-noise hint: only shown when the matcher set the flag.
    if (row.noiseAuto) {
      const hint = document.createElement("span");
      hint.className = "voice-row-noise-hint";
      hint.textContent = t("voices.row.noise_auto_hint");
      body.appendChild(hint);
    }

    li.appendChild(body);
    voiceListEl.appendChild(li);
  });
}

// Rebind-with-fragment-rollback confirm (spec "Откат фрагмента при
// перепривязке"): fires only when overriding a binding that THIS dialog
// session already saved with a fragment for this task. The actual rollback
// (deleting the VoiceSample whose source_task_id == this task) happens
// server-side keyed off source_task_id; this is only the UI confirmation.
function onVoiceRowRebind(row, newValue, previousValue) {
  if (
    row.savedBinding &&
    row.savedBinding.addedFragment &&
    row.savedBinding.speaker_id === previousValue &&
    newValue !== previousValue
  ) {
    const prevName = (row.options.find((o) => o.speaker_id === previousValue) || {}).name || previousValue;
    if (!window.confirm(t("voices.confirm.rollback", { name: prevName }))) {
      // Revert the <select> back to its previous value without applying the change.
      const selectEl = voiceListEl?.querySelector(
        `[data-speaker-label="${CSS.escape(row.label)}"] .voice-select`
      );
      if (selectEl) selectEl.value = previousValue;
      return;
    }
  }
  row.selection = newValue;
}

// Maps outcome + prior/current binding to the MatchDecision.outcome the
// backend expects (see the spec's "Исходы" table). Mirrors it exactly so the
// calibration data the backend accumulates is meaningful.
function resolveOutcomeCode(row) {
  const boundExisting = row.selection !== NEW_PERSON_VALUE;
  if (row.outcome === "miss") {
    return boundExisting ? "manual_match" : "left_anonymous";
  }
  if (row.outcome === "auto") {
    // matchedSpeakerId is the auto-bound candidate; unchanged selection = accepted.
    return boundExisting && row.selection === row.matchedSpeakerId ? "auto_accepted" : "auto_overridden";
  }
  // grey
  if (!boundExisting) return "left_anonymous";
  return row.selection === row.options[0]?.speaker_id ? "confirmed" : "rejected";
}

function buildResolutions() {
  return voiceDialogState.rows.map((row) => {
    const bindingNew = row.selection === NEW_PERSON_VALUE;
    const outcomeCode = resolveOutcomeCode(row);
    const distance = row.options.find((o) => o.speaker_id === row.selection);
    const base = {
      speaker_label: row.label,
      outcome: outcomeCode,
      distance: distance && typeof distance.distance === "number" ? distance.distance : row.matchedDistance,
      is_noise: row.noise,
    };
    if (bindingNew) {
      if (row.newName.trim()) {
        return { ...base, action: "bind_new", new_name: row.newName.trim(), add_fragment: true };
      }
      return { ...base, action: "leave_anonymous", add_fragment: false };
    }
    if (row.outcome === "auto" && row.selection === row.matchedSpeakerId) {
      return { ...base, action: "accept_auto", speaker_id: row.selection, add_fragment: row.addFragment };
    }
    return { ...base, action: "bind_existing", speaker_id: row.selection, add_fragment: row.addFragment };
  });
}

function anyVoiceLeftAnonymous() {
  return voiceDialogState.rows.some(
    (row) => row.selection === NEW_PERSON_VALUE && !row.newName.trim()
  );
}

// Edit-after-summarization confirm (spec "Правка после начала суммаризации"):
// fires only if the task's summary has already started/finished. Reuses the
// same task list the main render loop already fetched rather than a fresh
// request — the dialog is opened from a rendered task row.
function taskSummaryStarted(taskId) {
  const taskEl = findTaskEl(taskId);
  const runtime = taskEl && taskEl._runtime;
  if (!runtime) return false;
  if (runtime.summaryReady) return true;
  const currentStep = String(runtime.currentStepName || "");
  return currentStep.startsWith("summarize") || currentStep.startsWith("finalize");
}

async function submitVoiceResolutions(continueTask) {
  if (!voiceDialogState) return;
  // Capture before closeVoiceDialog nulls voiceDialogState — used to re-fetch
  // the transcript tab after the save so renamed/noise speakers re-render.
  const voiceTaskId = voiceDialogState.taskId;
  if (continueTask && anyVoiceLeftAnonymous()) {
    if (!window.confirm(t("voices.confirm.anonymous"))) return;
  }
  // Only warn about post-summarization edits when this dialog is reopened on
  // a task that already has a saved binding from a prior visit (i.e. this is
  // truly an edit, not the first-time resolution before anything downstream ran).
  const hadPriorSave = voiceDialogState.rows.some((row) => row.savedBinding !== null);
  if (hadPriorSave && taskSummaryStarted(voiceDialogState.taskId)) {
    if (!window.confirm(t("voices.confirm.edit_after_summary"))) return;
  }
  const resolutions = buildResolutions();
  try {
    await api(`/api/tasks/${encodeURIComponent(voiceDialogState.taskId)}/speakers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolutions, continue_task: continueTask }),
    });
  } catch (err) {
    console.error("Failed to save voice resolutions", err);
    return;
  }
  // Record what was just saved so a later rebind in the same session (dialog
  // reopened without a page reload) can detect the rollback case, and reset
  // dirty tracking to the just-saved state.
  voiceDialogState.rows.forEach((row) => {
    row.savedBinding = row.selection !== NEW_PERSON_VALUE
      ? { speaker_id: row.selection, addedFragment: row.addFragment }
      : null;
    row.initialSelection = row.selection;
  });
  closeVoiceDialog({ skipConfirm: true });
  // Refresh this task IN PLACE (not loadTasks(), which rebuilds the whole list
  // and collapses the card — bug #3, vts-552). Preserves the expanded card and
  // its active tab.
  await refreshTaskInPlace(voiceTaskId);
  // Re-render the raw transcript for this task if it's the active tab, so the
  // just-saved speaker names / noise flags take effect without a manual reload.
  const taskEl = findTaskEl(voiceTaskId);
  if (taskEl && getActiveTabName(taskEl) === "transcript") {
    await loadTabContent(taskEl, voiceTaskId, "transcript");
  }
  // The card's speakers panel shows the same bindings this dialog just changed.
  // Without this the two disagree on screen until the card is collapsed and
  // reopened. Only reload a panel that has already been populated — an untouched
  // card should stay lazy.
  if (taskEl && taskEl._speakerRows) {
    await loadSpeakerPanel(taskEl, voiceTaskId);
  }
}

function closeVoiceDialog(opts = {}) {
  if (!opts.skipConfirm && isVoiceDialogDirty()) {
    if (!window.confirm(t("voices.confirm.discard"))) return;
  }
  voiceDialogState = null;
  if (voiceDialog?.open) voiceDialog.close();
}

async function openVoiceDialog(taskId, paused) {
  if (!voiceDialog) return;
  let matches;
  let speakers;
  try {
    [matches, speakers] = await Promise.all([
      api(`/api/tasks/${encodeURIComponent(taskId)}/speaker-matches`),
      api("/api/speakers"),
    ]);
  } catch (err) {
    console.error("Failed to load voice matches", err);
    return;
  }
  const allSpeakers = Array.isArray(speakers) ? speakers : [];
  const labels = Object.keys(matches || {}).sort();
  voiceDialogState = {
    taskId,
    paused: Boolean(paused),
    // Rows sorted by speaking share, loudest first (vts-552).
    rows: labels
      .map((label) => buildVoiceRow(label, matches[label] || {}, allSpeakers))
      .sort((a, b) => b.share - a.share),
  };
  renderVoiceList();
  if (typeof voiceDialog.showModal === "function") {
    voiceDialog.showModal();
  } else {
    voiceDialog.setAttribute("open", "");
  }
  // "Save & continue" only makes sense on a paused (awaiting_input) task —
  // hide it when resolving/editing on a task that isn't waiting (vts-552).
  if (voiceSaveContinueBtn) {
    voiceSaveContinueBtn.classList.toggle("hidden", !voiceDialogState.paused);
  }
}

voiceSaveBtn?.addEventListener("click", () => {
  void submitVoiceResolutions(false);
});
voiceSaveContinueBtn?.addEventListener("click", () => {
  void submitVoiceResolutions(true);
});
voiceCancelBtn?.addEventListener("click", () => {
  closeVoiceDialog();
});
document.getElementById("voice-close-btn")?.addEventListener("click", () => {
  closeVoiceDialog();
});
voiceDialog?.addEventListener("cancel", (event) => {
  // Esc fires the native `cancel` event before closing; intercept so the
  // dirty-check confirm runs (backdrop click and the close button already go
  // through closeVoiceDialog via their own handlers, but Esc bypasses those).
  event.preventDefault();
  closeVoiceDialog();
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
const presetEditDiarize = document.getElementById("preset-edit-diarize");
const presetEditSpeakerNoManualStop = document.getElementById("preset-edit-speaker_no_manual_stop");
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

// Same dependency as the create form's pill (syncSpeakerNoManualStopToggle):
// meaningless without diarize, never cleared on disable (only dimmed) so a
// stray toggle doesn't mark the preset dirty.
function syncPresetSpeakerNoManualStopToggle() {
  if (!presetEditSpeakerNoManualStop) return;
  const disabled = !(presetEditDiarize && presetEditDiarize.checked);
  presetEditSpeakerNoManualStop.disabled = disabled;
  const pill = document.getElementById("preset-edit-speaker-no-manual-stop-pill");
  if (pill) pill.classList.toggle("disabled", disabled);
}

function resetPresetForm() {
  if (presetNameInput) presetNameInput.value = "";
  if (presetEditLanguage) presetEditLanguage.value = "";
  if (presetEditAudioOnly) presetEditAudioOnly.checked = false;
  if (presetEditTranscript) presetEditTranscript.checked = true;
  if (presetEditDiarize) presetEditDiarize.checked = false;
  if (presetEditSpeakerNoManualStop) presetEditSpeakerNoManualStop.checked = false;
  syncPresetSpeakerNoManualStopToggle();
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
  if (presetEditDiarize) presetEditDiarize.checked = !!opts.diarize;
  if (presetEditSpeakerNoManualStop) presetEditSpeakerNoManualStop.checked = !!opts.speaker_no_manual_stop;
  syncPresetSpeakerNoManualStopToggle();
  if (presetEditPrompts) {
    const { filtered } = filterDanglingPrompts(opts.prompts);
    renderPromptMultiselect(presetEditPrompts, promptsCache, filtered, {
      flat: true,
    });
  }
  // Destinations that no longer exist are dropped rather than rendered as
  // ghost rows: the preset keeps working, and re-saving prunes them.
  renderPresetDeliverySelect(
    (opts.delivery || []).filter((d) =>
      deliveryTargetsList().some((t) => t.id === d.deliver_to)
    )
  );
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

presetEditDiarize?.addEventListener("change", syncPresetSpeakerNoManualStopToggle);

presetForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const editId = presetEditIdInput?.value || "";
  const name = (presetNameInput?.value || "").trim();
  if (!name) return;
  const options = {
    language: presetEditLanguage ? presetEditLanguage.value || "" : "",
    audio_only: !!(presetEditAudioOnly && presetEditAudioOnly.checked),
    transcript: !!(presetEditTranscript && presetEditTranscript.checked),
    diarize: !!(presetEditDiarize && presetEditDiarize.checked),
    speaker_no_manual_stop: !!(presetEditSpeakerNoManualStop && presetEditSpeakerNoManualStop.checked),
    prompts: getSelectedFrom(presetEditPrompts),
    delivery: selectedDeliveryRefs(presetDeliverySelect),
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
  // Write into the label span, never the button: the button also holds an <svg>
  // icon that assigning textContent would destroy (vts-nr4).
  const pushLabel = pushToggleBtn.querySelector("span");
  if (pushLabel) pushLabel.textContent = label;
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

// ---------- Header burger menu ----------

const headerMenuBtn = document.getElementById("header-menu-btn");
const headerMenu = document.getElementById("header-menu");
if (headerMenuBtn && headerMenu) {
  headerMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = headerMenu.classList.contains("open");
    document.querySelectorAll(".btn-menu.open").forEach((m) => m.classList.remove("open"));
    if (!isOpen) {
      // Same fixed-position placement the task cards' menus use: measure the
      // trigger, then right-align the panel to it so a wide menu never hangs
      // off the screen edge.
      const rect = headerMenuBtn.getBoundingClientRect();
      headerMenu.style.top = `${rect.bottom + 4}px`;
      headerMenu.style.left = "0px";
      headerMenu.classList.add("open");
      headerMenu.style.left = `${Math.max(8, rect.right - headerMenu.offsetWidth)}px`;
    }
    headerMenuBtn.setAttribute("aria-expanded", String(!isOpen));
  });
  // Closing on entry click is handled by the document-level listener, which
  // knows which entries are in-place toggles (MENU_KEEPS_OPEN).
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
  syncLocaleControl();
  renderFilterTypeSegments();
  setVersionLabel(BUILD_VERSION);
  syncSummaryToggle();
  syncSourceType();
  applySharedUrlIfAny();
  await applyPendingSharedFileIfAny();
  // Load prompts before the first task render so per-prompt finalize step
  // labels resolve to names (not the raw "finalize:user:<uuid>") on first paint.
  await loadPrompts();
  // Restore BEFORE the first task fetch, so the initial request already
  // carries the filters a reload is expected to preserve. Restoring after
  // refreshAll() would load an unfiltered list and then contradict it.
  restoreFilters();
  await refreshAll();
  // Infinite scroll: observe the sentinel once at bootstrap (refreshAll()
  // re-runs on user switches, so wiring here avoids duplicate observers).
  const taskSentinelObserver = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) void loadNextPage();
  }, { rootMargin: "200px" });
  const _sentinelEl = document.getElementById("task-sentinel");
  if (_sentinelEl) taskSentinelObserver.observe(_sentinelEl);
  await loadDeliveryEntities();
  renderDeliverySelectors();
  await loadPresets();
  await loadPushConfig();
  await loadProgressWeights();
  await loadUploadConfig();
}

// ---------------------------------------------------------------------------
// Delivery: connections (credentials) and destinations (targets) — vts-j2kh
//
// The browser cannot import adapters, so /api/delivery-adapters serves each
// adapter's JSON Schema plus the names of the fields that form its CONNECTION.
// Forms are generated from that: connection fields go on the credential,
// everything else on the target. The core never assumes what a connection is
// (vts-929) and neither does this UI.
// ---------------------------------------------------------------------------

const deliveryDialog = document.getElementById("delivery-dialog");
const deliverySelect = document.getElementById("delivery-select");
const deliverySelectField = document.getElementById("delivery-select-field");
const presetDeliverySelect = document.getElementById("preset-edit-delivery");
const presetDeliveryField = document.getElementById("preset-delivery-field");

/** Destinations known right now.
 *
 * Bootstrap calls loadPresets() -> applyPresetOptions() before delivery data
 * has been fetched, so every reader must tolerate "not loaded yet" rather
 * than assume an array is already in place.
 */
function deliveryTargetsList() {
  return Array.isArray(deliveryState.targets) ? deliveryState.targets : [];
}

function deliveryAdapter(name) {
  return deliveryState.adapters.find((a) => a.name === name) || null;
}

/** Schema properties split into connection fields and per-destination ones. */
function splitSchemaFields(adapter, { connection }) {
  if (!adapter) return [];
  const schema = adapter.config_schema || {};
  const props = schema.properties || {};
  const required = new Set(schema.required || []);
  const isConnection = new Set(adapter.connection_fields || []);
  const secrets = new Set(adapter.secret_keys || []);

  const out = [];
  for (const [name, spec] of Object.entries(props)) {
    if (isConnection.has(name) !== connection) continue;
    out.push({
      name,
      spec: spec || {},
      required: required.has(name),
      secret: secrets.has(name),
    });
  }
  // Secret keys are declared separately from config_schema, so an adapter may
  // list a secret that has no schema entry (Outline's api_token does exactly
  // this). Without these the credential form would have no password field.
  if (connection) {
    for (const key of secrets) {
      if (!isConnection.has(key)) continue;
      if (out.some((f) => f.name === key)) continue;
      out.push({ name: key, spec: { type: "string" }, required: false, secret: true });
    }
  }
  return out;
}

/** Human label for a config field, in the user's language where possible.
 *
 * Order matters, and so does why:
 *   1. an i18n key for a well-known field name — the ONLY multilingual
 *      option, because the locale lives in the browser and the server never
 *      sees it, so a plugin's schema string can only ever be one language;
 *   2. the schema's own `title`, which lets a plugin name a field we do not
 *      know about (in whatever language it chose);
 *   3. the raw key, so a field is never unlabelled.
 */
function deliveryFieldLabel(field) {
  const key = `delivery.field.${field.name}`;
  const translated = t(key);
  if (translated !== key) return translated;
  return (field.spec || {}).title || field.name;
}

/** Build one input for a schema property. Returns the element to read from. */
function buildSchemaInput(field, value) {
  const spec = field.spec || {};
  let input;
  if (Array.isArray(field.options)) {
    // Values enumerated by the adapter: {value, label} pairs, so the name is
    // shown while the stable id is stored (vts-6o37). The enum branch below
    // cannot express this — there value and label are the same string.
    input = document.createElement("select");
    for (const option of field.options) {
      const el = document.createElement("option");
      el.value = option.value;
      el.textContent = option.label;
      input.appendChild(el);
    }
    if (value !== undefined && value !== null) input.value = String(value);
  } else if (Array.isArray(spec.enum)) {
    input = document.createElement("select");
    // A non-required enum needs an empty choice, else the first option is
    // silently submitted for a field the user never touched.
    if (!field.required) {
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "—";
      input.appendChild(blank);
    }
    for (const option of spec.enum) {
      const el = document.createElement("option");
      el.value = String(option);
      el.textContent = String(option);
      input.appendChild(el);
    }
    if (value !== undefined && value !== null) input.value = String(value);
  } else if (spec.type === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else if (spec.type === "integer" || spec.type === "number") {
    input = document.createElement("input");
    input.type = "number";
    if (spec.type === "integer") input.step = "1";
    if (value !== undefined && value !== null) input.value = String(value);
  } else if (spec.type === "object" || spec.type === "array") {
    // No guessing at a widget for a composite type: an editor that cannot
    // represent the value would silently drop part of it. Show the JSON.
    input = document.createElement("textarea");
    input.rows = 3;
    input.dataset.json = "1";
    if (value !== undefined) input.value = JSON.stringify(value, null, 2);
  } else {
    input = document.createElement("input");
    input.type = field.secret ? "password" : "text";
    if (value !== undefined && value !== null) input.value = String(value);
  }
  input.dataset.field = field.name;
  if (field.secret) input.dataset.secret = "1";
  if (field.required) input.required = true;
  if (spec.description) input.title = spec.description;
  return input;
}

/** Ask the adapter to enumerate a field's values, via the core.
 *
 * Returns null when the external system could not be reached. There is
 * deliberately NO free-text fallback (Victor, 2026-08-05) — the caller shows
 * an explicit message instead, because a picker that quietly becomes a text
 * box hides why it did, and an empty list is indistinguishable from "there
 * are none".
 */
async function fetchFieldOptions(credentialId, field) {
  if (!credentialId) return null;
  try {
    const body = await api(
      `/api/delivery-credentials/${credentialId}/options/${encodeURIComponent(field)}`
    );
    return Array.isArray(body?.options) ? body.options : [];
  } catch (err) {
    return { error: deliveryErrorText(err, "delivery.options.unavailable") };
  }
}

function renderSchemaFields(container, adapter, { connection, values, secretsSet, options }) {
  if (!container) return;
  container.innerHTML = "";
  const fields = splitSchemaFields(adapter, { connection });
  for (const field of fields) {
    // Adapter-enumerated values, when the core managed to fetch them.
    if (options && Array.isArray(options[field.name])) field.options = options[field.name];
    const row = document.createElement("label");
    row.className = "delivery-field";

    const label = document.createElement("span");
    label.className = "delivery-field-label";
    label.textContent = deliveryFieldLabel(field) + (field.required ? " *" : "");

    const input = buildSchemaInput(field, (values || {})[field.name]);
    // The check button rides alongside the endpoint field rather than sitting
    // in the form actions: it tests THAT value, and putting it there keeps it
    // out of the primary/secondary button row where its colour states would
    // clash with the submit button (vts-6o37 followup).
    const attachCheck = connection && field.name === (adapter?.connection_fields || [])[0];
    if (field.secret) {
      // Values of stored secrets are never served; only whether one is set.
      const isSet = Boolean((secretsSet || {})[field.name]?.set);
      input.placeholder = isSet
        ? t("delivery.secret.set")
        : t("delivery.secret.unset");
    }
    if (attachCheck) {
      const wrap = document.createElement("span");
      wrap.className = "delivery-field-with-check";
      wrap.append(input, buildDeliveryCheckButton());
      row.append(label, wrap);
    } else {
      row.append(label, input);
    }
    container.appendChild(row);
  }
}

/** Read a generated form back into a config object (+ secrets separately). */
function readSchemaFields(container) {
  const config = {};
  const secrets = {};
  if (!container) return { config, secrets };
  container.querySelectorAll("[data-field]").forEach((input) => {
    const name = input.dataset.field;
    let value;
    if (input.type === "checkbox") {
      value = input.checked;
    } else if (input.dataset.json === "1") {
      const raw = (input.value || "").trim();
      if (!raw) return;
      try {
        value = JSON.parse(raw);
      } catch {
        // Leave it out rather than send something the adapter cannot read;
        // the server validates against the schema and will say what is wrong.
        return;
      }
    } else if (input.type === "number") {
      const raw = (input.value || "").trim();
      if (raw === "") return;
      value = Number(raw);
    } else {
      const raw = input.value || "";
      // An untouched password field means "keep what is stored", not "clear".
      if (raw === "") return;
      value = raw;
    }
    if (input.dataset.secret === "1") {
      secrets[name] = value;
    } else {
      config[name] = value;
    }
  });
  return { config, secrets };
}

async function loadDeliveryAdapters() {
  try {
    const body = await api("/api/delivery-adapters");
    deliveryState.adapters = body.adapters || [];
    deliveryState.incompatible = body.incompatible || {};
    // Offered by the core, not by any adapter (vts-6fya) — includes this
    // user's own prompts, so it cannot come from a plugin's static schema.
    deliveryState.variants = Array.isArray(body.variants) ? body.variants : [];
  } catch (err) {
    console.error("Failed to load delivery adapters", err);
    deliveryState.adapters = [];
    deliveryState.incompatible = {};
  }
}

async function loadDeliveryEntities() {
  try {
    const [credentials, targets] = await Promise.all([
      api("/api/delivery-credentials"),
      api("/api/delivery-targets"),
    ]);
    // Array.isArray, not `|| []`: api() returns a STRING for a non-JSON
    // response, and a string is truthy — it would survive the fallback and
    // then blow up in `for...of` / `.some()` at the call sites.
    deliveryState.credentials = Array.isArray(credentials) ? credentials : [];
    deliveryState.targets = Array.isArray(targets) ? targets : [];
  } catch (err) {
    console.error("Failed to load delivery settings", err);
    deliveryState.credentials = [];
    deliveryState.targets = [];
  }
}

function deliveryRowActions(onEdit, onDelete) {
  const actions = document.createElement("div");
  actions.className = "prompts-actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "icon-btn ghost";
  edit.setAttribute("data-tooltip", t("delivery.form.save"));
  edit.setAttribute("aria-label", t("delivery.form.save"));
  // innerHTML, not textContent: applyI18n would wipe an SVG child, and these
  // buttons are icon-only (see the prompts list for the same construction).
  edit.innerHTML = ICON_EDIT;
  edit.addEventListener("click", onEdit);
  const del = document.createElement("button");
  del.type = "button";
  del.className = "icon-btn ghost danger";
  del.setAttribute("data-tooltip", t("prompts.manage.delete"));
  del.setAttribute("aria-label", t("prompts.manage.delete"));
  del.innerHTML = ICON_DELETE;
  del.addEventListener("click", onDelete);
  actions.append(edit, del);
  return actions;
}

function renderDeliveryCredentials() {
  const list = document.getElementById("delivery-credentials-list");
  if (!list) return;
  list.innerHTML = "";
  for (const cred of deliveryState.credentials) {
    const row = document.createElement("div");
    row.className = "tokens-row prompts-row";

    const main = document.createElement("div");
    main.className = "tokens-meta prompts-meta";
    const name = document.createElement("span");
    name.className = "tokens-name prompt-name";
    name.textContent = cred.name;
    const meta = document.createElement("span");
    meta.className = "delivery-meta";
    meta.textContent = cred.adapter
      + " · " + t("delivery.used_by", { count: cred.used_by || 0 });
    main.append(name, meta);
    if (!cred.adapter_available) {
      const warn = document.createElement("span");
      warn.className = "delivery-unavailable";
      warn.textContent = t("delivery.adapter_missing");
      main.appendChild(warn);
    }

    row.append(main, deliveryRowActions(
      () => editDeliveryCredential(cred),
      () => deleteDeliveryCredential(cred),
    ));
    list.appendChild(row);
  }
}

function renderDeliveryTargets() {
  const list = document.getElementById("delivery-targets-list");
  if (!list) return;
  list.innerHTML = "";
  for (const target of deliveryTargetsList()) {
    const row = document.createElement("div");
    row.className = "tokens-row prompts-row";

    const main = document.createElement("div");
    main.className = "tokens-meta prompts-meta";
    const name = document.createElement("span");
    name.className = "tokens-name prompt-name";
    name.textContent = target.name;
    const cred = deliveryState.credentials.find((c) => c.id === target.credential_id);
    const meta = document.createElement("span");
    meta.className = "delivery-meta";
    meta.textContent = target.adapter + " · " + (cred ? cred.name : "—");
    main.append(name, meta);
    if (!target.adapter_available) {
      const warn = document.createElement("span");
      warn.className = "delivery-unavailable";
      warn.textContent = t("delivery.adapter_missing");
      main.appendChild(warn);
    }

    row.append(main, deliveryRowActions(
      () => editDeliveryTarget(target),
      () => deleteDeliveryTarget(target),
    ));
    list.appendChild(row);
  }
}

function fillAdapterSelect() {
  const select = document.getElementById("delivery-credential-adapter");
  if (!select) return;
  const previous = select.value;
  select.innerHTML = "";
  for (const adapter of deliveryState.adapters) {
    const option = document.createElement("option");
    option.value = adapter.name;
    option.textContent = adapter.name;
    select.appendChild(option);
  }
  if (previous && deliveryAdapter(previous)) select.value = previous;
}

/** Connections offered for a target, filtered to the target's adapter: an
 *  Outline token cannot authenticate an S3 destination, and the server
 *  rejects the mismatch anyway. */
function fillCredentialSelect(adapterName, selectedId) {
  const select = document.getElementById("delivery-target-credential");
  if (!select) return;
  select.innerHTML = "";
  const usable = deliveryState.credentials.filter(
    (c) => !adapterName || c.adapter === adapterName
  );
  for (const cred of usable) {
    const option = document.createElement("option");
    option.value = cred.id;
    option.textContent = cred.name + " (" + cred.adapter + ")";
    select.appendChild(option);
  }
  if (selectedId) select.value = selectedId;
}

function resetDeliveryCredentialForm() {
  document.getElementById("delivery-credential-edit-id").value = "";
  document.getElementById("delivery-credential-name").value = "";
  document.getElementById("delivery-credential-submit").textContent =
    t("delivery.credentials.create");
  document.getElementById("delivery-credential-cancel").classList.add("hidden");
  resetDeliveryCheck();
  const adapterName = document.getElementById("delivery-credential-adapter")?.value;
  renderSchemaFields(
    document.getElementById("delivery-credential-fields"),
    deliveryAdapter(adapterName),
    { connection: true, values: {}, secretsSet: {} },
  );
}

/** Fill the target form's variant picker from the core-supplied list. */
function fillVariantSelect(selected) {
  const select = document.getElementById("delivery-target-variant");
  if (!select) return;
  select.innerHTML = "";
  for (const variant of deliveryState.variants || []) {
    const option = document.createElement("option");
    option.value = variant.value;
    option.textContent = variant.label.startsWith("delivery.variant.")
      ? t(variant.label)
      : variant.label;
    select.appendChild(option);
  }
  // "summary" is the core's own fallback when a target names no variant
  // (see vts/delivery/queue.py), so the form opens on the same choice the
  // backend would have made.
  select.value = selected || "summary";
}

/** Render the target form, turning enumerable fields into pickers.
 *
 * Async because the values live in the external system. When it cannot be
 * reached the field is still rendered (as text) but an explicit message says
 * so — no silent degradation.
 */
async function renderTargetFields(adapter, credentialId, values) {
  const container = document.getElementById("delivery-target-fields");
  const notice = document.getElementById("delivery-target-notice");
  if (notice) {
    notice.hidden = true;
    notice.textContent = "";
  }
  renderSchemaFields(container, adapter, {
    connection: false, values: values || {}, secretsSet: {},
  });
  if (!adapter || !credentialId) return;

  const enumerable = (adapter.option_fields || []);
  if (!enumerable.length) return;

  const problems = [];
  const resolved = {};
  for (const field of enumerable) {
    const result = await fetchFieldOptions(credentialId, field);
    if (result && result.error) {
      problems.push(`${field}: ${result.error}`);
    } else if (Array.isArray(result)) {
      resolved[field] = result;
    }
  }
  if (Object.keys(resolved).length) {
    renderSchemaFields(container, adapter, {
      connection: false, values: values || {}, secretsSet: {}, options: resolved,
    });
  }
  if (problems.length && notice) {
    notice.textContent = problems.join("; ");
    notice.hidden = false;
  }
}

function resetDeliveryTargetForm() {
  document.getElementById("delivery-target-edit-id").value = "";
  document.getElementById("delivery-target-name").value = "";
  document.getElementById("delivery-target-submit").textContent =
    t("delivery.targets.create");
  document.getElementById("delivery-target-cancel").classList.add("hidden");
  const cred = deliveryState.credentials[0];
  fillVariantSelect("summary");
  fillCredentialSelect(cred ? cred.adapter : null, cred ? cred.id : null);
  void renderTargetFields(
    deliveryAdapter(cred ? cred.adapter : null), cred ? cred.id : null, {},
  );
}

function editDeliveryCredential(cred) {
  document.getElementById("delivery-credential-edit-id").value = cred.id;
  document.getElementById("delivery-credential-name").value = cred.name;
  const adapterSelect = document.getElementById("delivery-credential-adapter");
  if (adapterSelect) adapterSelect.value = cred.adapter;
  document.getElementById("delivery-credential-submit").textContent =
    t("delivery.form.save");
  document.getElementById("delivery-credential-cancel").classList.remove("hidden");
  resetDeliveryCheck();
  renderSchemaFields(
    document.getElementById("delivery-credential-fields"),
    deliveryAdapter(cred.adapter),
    { connection: true, values: cred.config || {}, secretsSet: cred.secrets || {} },
  );
}

function editDeliveryTarget(target) {
  document.getElementById("delivery-target-edit-id").value = target.id;
  document.getElementById("delivery-target-name").value = target.name;
  fillVariantSelect((target.config || {}).default_variant);
  fillCredentialSelect(target.adapter, target.credential_id);
  document.getElementById("delivery-target-submit").textContent =
    t("delivery.form.save");
  document.getElementById("delivery-target-cancel").classList.remove("hidden");
  void renderTargetFields(
    deliveryAdapter(target.adapter), target.credential_id, target.config || {},
  );
}

/** Pull the server's message out of an api() error.
 *
 * api() throws Error("<status>: <body>") where body is the JSON error
 * envelope, so the useful part (e.g. "Credential is used by 2 delivery
 * target(s)") needs digging out. Falls back to a generic message. */
function deliveryErrorText(err, fallbackKey) {
  const raw = String(err?.message || "");
  const jsonStart = raw.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart));
      if (parsed && parsed.detail) return String(parsed.detail);
    } catch {
      // fall through to the generic message
    }
  }
  return t(fallbackKey);
}

async function deleteDeliveryCredential(cred) {
  if (!window.confirm(t("delivery.credentials.confirm_delete", { name: cred.name }))) {
    return;
  }
  try {
    await api(`/api/delivery-credentials/${cred.id}`, { method: "DELETE" });
  } catch (err) {
    // A connection still in use comes back as 409 with a count. Surface the
    // server's message rather than a generic failure: the fix is "remove
    // those destinations first", which the count tells the user.
    window.alert(deliveryErrorText(err, "delivery.credentials.delete_failed"));
    return;
  }
  await refreshDeliveryManager();
}

async function deleteDeliveryTarget(target) {
  if (!window.confirm(t("delivery.targets.confirm_delete", { name: target.name }))) {
    return;
  }
  try {
    await api(`/api/delivery-targets/${target.id}`, { method: "DELETE" });
  } catch (err) {
    window.alert(deliveryErrorText(err, "delivery.targets.delete_failed"));
    return;
  }
  await refreshDeliveryManager();
}

async function refreshDeliveryManager() {
  await loadDeliveryEntities();
  renderDeliveryCredentials();
  renderDeliveryTargets();
  resetDeliveryCredentialForm();
  resetDeliveryTargetForm();
  renderDeliverySelectors();
}

// --- selectors in the new-task card and in a preset -------------------------

/** One selectable destination: a checkbox plus its own variant picker.
 *
 * The variant is PER destination, not one setting for the whole task: two
 * collections on the same Outline can legitimately receive different
 * artifacts, which is the case vts-929 exists to support.
 */
/** One selectable destination.
 *
 * No per-delivery variant picker: which artifact a destination receives is a
 * property OF THE DESTINATION, set once when the target is configured
 * (vts-6fya). Sending two different artifacts to the same place is not a use
 * case — that is what a second target is for. The delivery entry is therefore
 * just {deliver_to}; the API still accepts an explicit `variant`, the UI
 * simply no longer sets one.
 */
function buildDeliveryRow(target, selected) {
  const chosen = selected.some((d) => d.deliver_to === target.id);
  const label = document.createElement("label");
  label.className = "prompt-row delivery-row";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = chosen;
  checkbox.dataset.targetId = target.id;
  // A destination whose plugin is not loaded cannot be submitted (the server
  // rejects it with 422), so it is shown but not selectable.
  checkbox.disabled = !target.adapter_available;

  const name = document.createElement("span");
  name.className = "prompt-name";
  name.textContent = target.name;

  // What this destination will send, so the choice is visible at the point of
  // use without being editable here.
  const variant = document.createElement("span");
  variant.className = "delivery-row-variant";
  variant.textContent = deliveryVariantLabel(
    (target.config || {}).default_variant || "summary"
  );

  label.append(checkbox, name, variant);
  if (!target.adapter_available) {
    const warn = document.createElement("span");
    warn.className = "delivery-unavailable";
    warn.textContent = t("delivery.adapter_missing");
    label.appendChild(warn);
  }
  return label;
}

/** Human name for a variant value, for display only. */
function deliveryVariantLabel(value) {
  const known = (deliveryState.variants || []).find((v) => v.value === value);
  if (known) {
    // Fixed variants carry an i18n key; a prompt carries its own name.
    return known.label.startsWith("delivery.variant.") ? t(known.label) : known.label;
  }
  return value;
}

function renderDeliveryMultiselect(container, selected) {
  if (!container) return;
  const chosen = Array.isArray(selected) ? selected : [];
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
  for (const target of deliveryTargetsList()) {
    popover.appendChild(buildDeliveryRow(target, chosen));
  }

  toggle.addEventListener("click", () => togglePromptPopover(container));
  popover.addEventListener("change", () => updateDeliverySummary(container));

  container.append(toggle, popover);
  updateDeliverySummary(container);
}

function updateDeliverySummary(container) {
  const summary = container?.querySelector(".prompt-select-summary");
  if (!summary) return;
  const count = container.querySelectorAll(
    '.prompt-select-popover input[type="checkbox"]:checked'
  ).length;
  summary.textContent = count
    ? t("delivery.selected_count", { count })
    : t("delivery.none_selected");
}

/** Read a delivery selector into the API's `delivery` list.
 *  deliver_to carries the target's ID, never its name (vts-929). */
function selectedDeliveryRefs(container) {
  if (!container) return [];
  const out = [];
  container
    .querySelectorAll('.prompt-select-popover input[type="checkbox"]:checked')
    .forEach((cb) => {
      // Just the destination: the variant belongs to the target itself
      // (vts-6fya), so nothing here overrides it.
      out.push({ deliver_to: cb.dataset.targetId });
    });
  return out;
}

/** Selectors stay hidden until at least one destination exists, so a user
 *  with no plugins never sees an empty control they cannot act on. */
function renderDeliverySelectors() {
  const has = deliveryTargetsList().length > 0;
  if (deliverySelectField) deliverySelectField.hidden = !has;
  if (presetDeliveryField) presetDeliveryField.hidden = !has;
  if (has) {
    renderDeliveryMultiselect(deliverySelect, selectedDeliveryRefs(deliverySelect));
  }
}

function renderPresetDeliverySelect(selected) {
  if (presetDeliveryField) {
    presetDeliveryField.hidden = deliveryTargetsList().length === 0;
  }
  renderDeliveryMultiselect(presetDeliverySelect, selected || []);
}

function resetDeliverySelection() {
  renderDeliveryMultiselect(deliverySelect, []);
}

// --- wiring -----------------------------------------------------------------

document.getElementById("delivery-btn")?.addEventListener("click", async () => {
  if (!deliveryDialog) return;
  await loadDeliveryAdapters();
  fillAdapterSelect();
  await refreshDeliveryManager();

  const noAdapters = document.getElementById("delivery-no-adapters");
  const sections = deliveryDialog.querySelector(".delivery-sections");
  const none = deliveryState.adapters.length === 0;
  if (noAdapters) noAdapters.hidden = !none;
  if (sections) sections.hidden = none;

  // Always open on connections: a destination cannot exist without one, so
  // that is where a first-time user has to start.
  showDeliveryTab("credentials");
  if (typeof deliveryDialog.showModal === "function") {
    deliveryDialog.showModal();
  } else {
    deliveryDialog.setAttribute("open", "");
  }
});

document.getElementById("delivery-close-btn")?.addEventListener("click", () => {
  deliveryDialog?.close();
});

/** Show one delivery tab and hide the other.
 *
 * `hidden` alone is not enough: `.delivery-section` sets `display`, which has
 * the same specificity as the browser's [hidden] default and beats it — the
 * exact trap that left the delivery selector visible with zero destinations
 * (vts-j2kh). The CSS carries a matching `[hidden] { display: none }` rule;
 * this only flips the attribute and the button state.
 */
function showDeliveryTab(name) {
  if (!deliveryDialog) return;
  deliveryDialog.querySelectorAll("[data-delivery-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.deliveryTab === name);
  });
  deliveryDialog.querySelectorAll("[data-delivery-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.deliveryPanel !== name;
  });
}

deliveryDialog?.querySelectorAll("[data-delivery-tab]").forEach((btn) => {
  btn.addEventListener("click", () => showDeliveryTab(btn.dataset.deliveryTab));
});

// --- connection check (vts-6o37) ---

const ICON_CHECK_OK = '<svg viewBox="0 0 24 24" aria-hidden="true" class="check-icon-ok"><path d="m5 13 4 4L19 7" /></svg>';
const ICON_CHECK_BAD = '<svg viewBox="0 0 24 24" aria-hidden="true" class="check-icon-bad"><circle cx="12" cy="12" r="9" /><path d="M12 7v6" /><path d="M12 16h.01" /></svg>';

/** The icon-only "test connection" button that sits beside the endpoint field.
 *
 * No visible label: it lives inline next to an input, where a word of text
 * would crowd the row. The name is carried by tooltip and aria-label instead
 * — and NOT via data-i18n, because applyI18n assigns textContent and would
 * wipe the inline SVG.
 */
function buildDeliveryCheckButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "delivery-check-btn";
  btn.className = "icon-btn ghost check-btn";
  btn.innerHTML = ICON_CHECK_OK;
  btn.setAttribute("data-tooltip", t("delivery.check.button"));
  btn.setAttribute("aria-label", t("delivery.check.button"));
  // Only meaningful for a SAVED connection: the check runs server-side
  // against stored secrets, so an unsaved form has no id to check.
  const editId = document.getElementById("delivery-credential-edit-id")?.value || "";
  const adapterName = document.getElementById("delivery-credential-adapter")?.value;
  if (!editId || !deliveryAdapter(adapterName)?.supports_check) {
    btn.classList.add("hidden");
  }
  btn.addEventListener("click", runDeliveryCheck);
  return btn;
}

/** Put the check button back to neutral.
 *
 * Called on ANY edit to the connection form: once a field changes, the last
 * result describes settings that no longer exist, and a stale green tick is
 * worse than no tick at all.
 */
function resetDeliveryCheck() {
  const btn = document.getElementById("delivery-check-btn");
  const msg = document.getElementById("delivery-check-message");
  if (btn) {
    btn.classList.remove("check-ok", "check-bad");
    btn.innerHTML = ICON_CHECK_OK;
  }
  if (msg) {
    msg.hidden = true;
    msg.textContent = "";
    msg.classList.remove("is-error");
  }
}

function showDeliveryCheckMessage(text, { error = false } = {}) {
  const msg = document.getElementById("delivery-check-message");
  if (!msg) return;
  msg.textContent = text;
  msg.classList.toggle("is-error", error);
  msg.hidden = false;
}

/** Whether the connection form currently describes a SAVED credential.
 *
 * The check runs server-side against stored secrets, so there has to be
 * something stored: an unsaved form has no id to check. Hence the button only
 * appears while editing an existing connection.
 */
async function runDeliveryCheck() {
  const btn = document.getElementById("delivery-check-btn");
  const editId = document.getElementById("delivery-credential-edit-id")?.value;
  if (!btn || !editId) return;

  resetDeliveryCheck();
  btn.disabled = true;
  showDeliveryCheckMessage(t("delivery.check.running"));
  let body;
  try {
    body = await api(`/api/delivery-credentials/${editId}/check`, { method: "POST" });
  } catch (err) {
    btn.disabled = false;
    btn.classList.add("check-bad");
    btn.innerHTML = ICON_CHECK_BAD;
    showDeliveryCheckMessage(deliveryErrorText(err, "delivery.check.failed"), { error: true });
    return;
  }
  btn.disabled = false;

  if (body?.ok) {
    btn.classList.add("check-ok");
    showDeliveryCheckMessage(t("delivery.check.ok"));
    return;
  }
  btn.classList.add("check-bad");
  btn.innerHTML = ICON_CHECK_BAD;
  // The server sends a CODE; the wording is ours, so it can be localised.
  // An unknown code still says something useful rather than nothing.
  const known = ["unreachable", "unauthorized", "not_found",
                 "unexpected_response", "timeout"];
  const key = known.includes(body?.outcome)
    ? `delivery.check.outcome.${body.outcome}`
    : "delivery.check.failed";
  const detail = body?.detail ? ` (${body.detail})` : "";
  showDeliveryCheckMessage(t(key) + detail, { error: true });
}

// Any edit invalidates the previous result — Victor's rule, applied to both
// the success and the failure state.
document.getElementById("delivery-credential-form")
  ?.addEventListener("input", resetDeliveryCheck);
document.getElementById("delivery-credential-form")
  ?.addEventListener("change", resetDeliveryCheck);

document.getElementById("delivery-credential-cancel")?.addEventListener("click", () => {
  resetDeliveryCredentialForm();
});

document.getElementById("delivery-target-cancel")?.addEventListener("click", () => {
  resetDeliveryTargetForm();
});

document.getElementById("delivery-credential-adapter")?.addEventListener("change", (event) => {
  renderSchemaFields(
    document.getElementById("delivery-credential-fields"),
    deliveryAdapter(event.target.value),
    { connection: true, values: {}, secretsSet: {} },
  );
});

document.getElementById("delivery-target-credential")?.addEventListener("change", (event) => {
  const cred = deliveryState.credentials.find((c) => c.id === event.target.value);
  void renderTargetFields(deliveryAdapter(cred?.adapter), cred?.id, {});
});

document.getElementById("delivery-credential-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const editId = document.getElementById("delivery-credential-edit-id").value;
  const name = document.getElementById("delivery-credential-name").value.trim();
  const adapterName = document.getElementById("delivery-credential-adapter").value;
  if (!name || !adapterName) return;

  const { config, secrets } = readSchemaFields(
    document.getElementById("delivery-credential-fields")
  );
  const payload = { name, config };
  // Omitting `secrets` keeps the stored ones; sending {} would be a no-op
  // anyway, but being explicit avoids ever clearing them by accident.
  if (Object.keys(secrets).length) payload.secrets = secrets;

  try {
    if (editId) {
      await api(`/api/delivery-credentials/${editId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/delivery-credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, adapter: adapterName }),
      });
    }
  } catch (err) {
    window.alert(deliveryErrorText(err, "delivery.credentials.save_failed"));
    return;
  }
  await refreshDeliveryManager();
});

document.getElementById("delivery-target-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const editId = document.getElementById("delivery-target-edit-id").value;
  const name = document.getElementById("delivery-target-name").value.trim();
  const credentialId = document.getElementById("delivery-target-credential").value;
  if (!name || !credentialId) return;
  const cred = deliveryState.credentials.find((c) => c.id === credentialId);

  const { config } = readSchemaFields(
    document.getElementById("delivery-target-fields")
  );
  // A core-owned key living in the same config blob as the adapter's own
  // settings; the server strips it before validating against the plugin
  // schema (vts-6fya).
  const variant = document.getElementById("delivery-target-variant")?.value;
  if (variant) config.default_variant = variant;
  const payload = { name, config, credential_id: credentialId };

  try {
    if (editId) {
      await api(`/api/delivery-targets/${editId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/delivery-targets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, adapter: cred?.adapter }),
      });
    }
  } catch (err) {
    window.alert(deliveryErrorText(err, "delivery.targets.save_failed"));
    return;
  }
  await refreshDeliveryManager();
});

// ---------------------------------------------------------------------------
// Task list filters — name / date / type (vts-rhx, VOS-84b)
//
// The filters narrow the same server-side cursor query the list already pages
// over, so filtering and infinite scroll compose: changing a filter resets
// paging (via the existing epoch mechanism) and every subsequent page carries
// the same filters.
//
// Kept in sessionStorage, not localStorage: a filter should survive a page
// reload (Victor, 2026-08-04) but a fresh tab must start unfiltered, so a
// half-empty list is never a mystery left over from days ago.
// ---------------------------------------------------------------------------

function currentFilters() {
  return {
    q: (filterInputs.q?.value || "").trim(),
    source_type: filterInputs.type?.value || "",
    created_from: filterInputs.from?.value || "",
    created_to: filterInputs.to?.value || "",
  };
}

function hasActiveFilters() {
  return Object.values(currentFilters()).some((v) => v !== "");
}

/** Append the active filters to a task-list query.
 *
 * A <input type="date"> yields "YYYY-MM-DD". `created_to` is pushed to the END
 * of that day, otherwise picking the same day for both bounds matches only
 * tasks created exactly at midnight — which reads as "the filter is broken".
 */
function appendFilterParams(params) {
  const f = currentFilters();
  if (f.q) params.set("q", f.q);
  if (f.source_type) params.set("source_type", f.source_type);
  if (f.created_from) params.set("created_from", `${f.created_from}T00:00:00`);
  if (f.created_to) params.set("created_to", `${f.created_to}T23:59:59`);
  return params;
}

function saveFilters() {
  try {
    const f = currentFilters();
    if (Object.values(f).every((v) => v === "")) {
      window.sessionStorage.removeItem(FILTERS_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(f));
    }
  } catch {
    // Private mode / storage disabled: filtering still works, it just does
    // not survive a reload. Never break the list over this.
  }
}

function restoreFilters() {
  let saved = null;
  try {
    saved = JSON.parse(window.sessionStorage.getItem(FILTERS_STORAGE_KEY) || "null");
  } catch {
    saved = null;
  }
  if (saved && typeof saved === "object") {
    if (filterInputs.q) filterInputs.q.value = saved.q || "";
    if (filterInputs.type) filterInputs.type.value = saved.source_type || "";
    if (filterInputs.from) filterInputs.from.value = saved.created_from || "";
    if (filterInputs.to) filterInputs.to.value = saved.created_to || "";
    // Same reason as the clear button: a restored filter has to show up on the
    // segmented skin, and .value assignment fires no event.
    renderFilterTypeSegments();
  }
  syncFilterChrome();
}

function syncFilterChrome() {
  const clearBtn = document.getElementById("filter-clear");
  if (clearBtn) clearBtn.classList.toggle("hidden", !hasActiveFilters());
}

/** Show "nothing matches" only when a filter is what emptied the list —
 *  a genuinely empty account is not a filtering problem. */
function updateEmptyState() {
  const empty = document.getElementById("task-empty");
  if (!empty) return;
  const noCards = !taskList.querySelector(".task");
  empty.hidden = !(noCards && hasActiveFilters());
}

/** Does this task belong in the list as currently filtered?
 *
 * SSE delivers events for EVERY task of the user, so without this a task
 * excluded by the filter would pop into a filtered list on its next update.
 * Mirrors the server's predicates; the server stays the authority for what a
 * page contains.
 */
function taskMatchesFilters(task) {
  const f = currentFilters();
  if (f.source_type) {
    const isFile = String(task.source_url || "").startsWith("file://");
    if (f.source_type === "file" && !isFile) return false;
    if (f.source_type === "url" && isFile) return false;
  }
  if (f.q) {
    const needle = f.q.toLowerCase();
    const haystack = `${task.source_title || ""} ${task.source_url || ""}`.toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  if (f.created_from && task.created_at) {
    if (new Date(task.created_at) < new Date(`${f.created_from}T00:00:00`)) return false;
  }
  if (f.created_to && task.created_at) {
    if (new Date(task.created_at) > new Date(`${f.created_to}T23:59:59`)) return false;
  }
  return true;
}

let filterDebounceTimer = null;

function applyFilters() {
  saveFilters();
  syncFilterChrome();
  // loadFirstPage() bumps the epoch, which discards any page fetch already in
  // flight for the previous filter set — reusing that instead of inventing a
  // second reset path.
  void loadFirstPage();
}

function onFilterChanged({ debounce = false } = {}) {
  if (filterDebounceTimer) window.clearTimeout(filterDebounceTimer);
  if (!debounce) {
    applyFilters();
    return;
  }
  // Typing must not fire a request per keystroke.
  filterDebounceTimer = window.setTimeout(applyFilters, 300);
}

// Segmented source-type filter: a skin over the hidden #filter-type <select>.
// Buttons are built FROM the select's options, so the labels follow
// applyI18nToPage() (each option carries its own data-i18n) and a new option
// needs no change here. The select keeps being the source of truth — writing to
// it and dispatching `change` is exactly what the native control did.
const filterTypeSeg = document.getElementById("filter-type-seg");

function renderFilterTypeSegments() {
  const select = filterInputs.type;
  if (!select || !filterTypeSeg) return;
  filterTypeSeg.textContent = "";
  for (const option of Array.from(select.options)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = option.textContent;
    btn.dataset.value = option.value;
    const active = option.value === select.value;
    btn.className = active ? "active" : "";
    btn.setAttribute("aria-pressed", String(active));
    btn.addEventListener("click", () => {
      if (select.value === option.value) return;
      select.value = option.value;
      // Programmatic .value does NOT fire change, so dispatch it: every filter
      // path (fetch, persistence, URL state) hangs off that event.
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    filterTypeSeg.appendChild(btn);
  }
}

// Keep the buttons in step whenever the select moves for any other reason:
// restore-from-storage, the clear button, or a future programmatic filter.
filterInputs.type?.addEventListener("change", renderFilterTypeSegments);

filterInputs.q?.addEventListener("input", () => onFilterChanged({ debounce: true }));
filterInputs.type?.addEventListener("change", () => onFilterChanged());
filterInputs.from?.addEventListener("change", () => onFilterChanged());
filterInputs.to?.addEventListener("change", () => onFilterChanged());

document.getElementById("filter-clear")?.addEventListener("click", () => {
  if (filterInputs.q) filterInputs.q.value = "";
  if (filterInputs.type) filterInputs.type.value = "";
  if (filterInputs.from) filterInputs.from.value = "";
  if (filterInputs.to) filterInputs.to.value = "";
  // Assigning .value does not fire `change`, so the segmented skin would keep
  // showing the cleared filter as active.
  renderFilterTypeSegments();
  onFilterChanged();
});

void bootstrap();

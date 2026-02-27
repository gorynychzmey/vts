const taskList = document.getElementById("task-list");
const taskTemplate = document.getElementById("task-template");
const form = document.getElementById("task-form");
const authUserLabel = document.getElementById("auth-user");
const actingUserLabel = document.getElementById("acting-user");
const adminPanel = document.getElementById("admin-panel");
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
  "summarize_windows",
  "summarize_final"
];
const SUMMARY_STEPS = new Set(["prepare_llama_model", "summarize_windows", "summarize_final"]);
const STEP_LABELS = {
  download: "Загрузка медиа",
  extract_audio: "Извлечение аудио",
  segment_audio: "Сегментация аудио",
  transcribe_segments: "Транскрибация сегментов",
  merge_transcript: "Сборка транскрипта",
  prepare_llama_model: "Подготовка LLM",
  summarize_windows: "Сводка по окнам",
  summarize_final: "Финальная сводка"
};

const state = {
  authUser: localStorage.getItem("vts_auth_user") || "demo@example.com",
  actingAs: localStorage.getItem("vts_as_user") || "",
  me: null,
  eventSource: null,
  versionTimer: null,
  durationTimer: null,
  queueTimer: null,
  queueRefreshInFlight: false
};

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

function stepLabel(stepName) {
  return STEP_LABELS[stepName] || stepName || "ожидание";
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
    return { value: 1, indeterminate: false, text: "ошибка" };
  }
  if (runtime.baseStatus === "queued") {
    if (runtime.queuePosition) {
      return { value: 0, indeterminate: false, text: `очередь #${runtime.queuePosition}` };
    }
    return { value: 0, indeterminate: false, text: "в очереди" };
  }

  const active = resolveActiveStep(runtime);
  let value = 0;
  let indeterminate = false;

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
      value = normalizeProgress(runtime.summary.current / runtime.summary.total);
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
    return { value: Math.max(0.05, value), indeterminate: true, text: "идет работа" };
  }
  return { value, indeterminate: false, text: `${Math.round(value * 100)}%` };
}

function setTaskStatusAppearance(statusEl, status, queuePosition = null) {
  if (status === "queued" && queuePosition) {
    statusEl.textContent = `queued #${queuePosition}`;
  } else {
    statusEl.textContent = status;
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
    elements.stepLabelEl.textContent = `Шаг ${normalizedIndex} из ${runtime.enabledSteps.length}: ${stepLabel(activeStep)}`;
  } else {
    elements.stepLabelEl.textContent = `Шаг - из ${runtime.enabledSteps.length}: ожидание`;
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

    root.dataset.taskId = task.id;
    transcriptPre.textContent = "Select tab to load transcript";
    summaryPre.textContent = "Select tab to load summary";
    logPre.textContent = "Select tab to load task log";

    toggleBtn.addEventListener("click", () => {
      body.classList.toggle("hidden");
      toggleBtn.classList.toggle("expanded", !body.classList.contains("hidden"));
      toggleBtn.title = body.classList.contains("hidden") ? "Expand" : "Collapse";
      toggleBtn.setAttribute("aria-label", body.classList.contains("hidden") ? "Expand" : "Collapse");
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
  const confirmed = window.confirm("Удалить задачу? Это действие необратимо.");
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
  authUserLabel.textContent = `${me.requested_by}${me.is_admin ? " (admin)" : ""}`;
  actingUserLabel.textContent = me.acting_as;
  if (!state.actingAs && me.acting_as !== me.requested_by) {
    state.actingAs = me.acting_as;
    localStorage.setItem("vts_as_user", state.actingAs);
  }
}

async function loadAdminPanel() {
  if (!state.me || !state.me.is_admin) {
    adminPanel.classList.add("hidden");
    return;
  }
  adminPanel.classList.remove("hidden");
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
adminApplyBtn.addEventListener("click", applyAdminUser);
adminResetBtn.addEventListener("click", resetAdminUser);

setVersionLabel(BUILD_VERSION);
syncSummaryToggle();
refreshAll();

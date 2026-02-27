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
const BUILD_VERSION = String(window.__VTS_BUILD_VERSION__ || "0.0.0");
const VERSION_CHECK_INTERVAL_MS = 30000;

const state = {
  authUser: localStorage.getItem("vts_auth_user") || "demo@example.com",
  actingAs: localStorage.getItem("vts_as_user") || "",
  me: null,
  eventSource: null,
  versionTimer: null
};

function setVersionLabel(version) {
  if (!appVersionLabel) {
    return;
  }
  const value = String(version || "").trim();
  appVersionLabel.textContent = value ? `v${value}` : "-";
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
    // Ignore transient network errors; next poll will retry.
  }
}

function startVersionWatcher() {
  if (state.versionTimer) {
    window.clearInterval(state.versionTimer);
  }
  state.versionTimer = window.setInterval(checkServerVersion, VERSION_CHECK_INTERVAL_MS);
}

function findRunningStep(steps) {
  if (!Array.isArray(steps)) {
    return "";
  }
  const running = steps.find((step) => step.status === "running");
  return running ? String(running.name || "") : "";
}

function renderTaskRuntime(taskEl) {
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  const runtime = taskEl._runtime;
  let statusText = runtime.baseStatus || "";
  if (runtime.currentStep) {
    statusText = `${statusText} · ${runtime.currentStep}`;
  }
  if (runtime.llamaStatus === "loading") {
    statusText = `${statusText} · LLM loading`;
  }
  if (runtime.mediaPhase) {
    statusText = `${statusText} · ${runtime.mediaPhase}`;
  }
  taskEl._elements.statusEl.textContent = statusText;
  taskEl._elements.llamaLoading.classList.toggle("hidden", runtime.llamaStatus !== "loading");
  taskEl._elements.downloadLoading.classList.toggle("hidden", !runtime.mediaPhase);
  if (runtime.mediaPhase) {
    taskEl._elements.downloadLoadingText.textContent = `Media ${runtime.mediaPhase}...`;
  }
}

function renderTasks(tasks) {
  taskList.innerHTML = "";
  tasks.forEach((task) => {
    const node = taskTemplate.content.cloneNode(true);
    const root = node.querySelector(".task");
    const header = node.querySelector(".task-header");
    const body = node.querySelector(".task-body");
    const urlEl = node.querySelector(".task-url");
    const statusEl = node.querySelector(".task-status");
    const videoProgress = node.querySelector(".video-progress");
    const audioProgress = node.querySelector(".audio-progress");
    const downloadLoading = node.querySelector(".download-loading");
    const downloadLoadingText = node.querySelector(".download-loading-text");
    const llamaLoading = node.querySelector(".llama-loading");
    const transcriptPre = node.querySelector(".transcript");
    const summaryPre = node.querySelector(".summary");
    const logPre = node.querySelector(".log");
    const currentStep = findRunningStep(task.steps);

    root.dataset.taskId = task.id;
    urlEl.textContent = task.source_url;
    transcriptPre.textContent = "Select tab to load transcript";
    summaryPre.textContent = "Select tab to load summary";
    logPre.textContent = "Select tab to load task log";

    header.addEventListener("click", () => body.classList.toggle("hidden"));
    node.querySelector(".pause-btn").addEventListener("click", () => updateTaskStatus(task.id, "pause"));
    node.querySelector(".resume-btn").addEventListener("click", () => updateTaskStatus(task.id, "resume"));
    node.querySelector(".delete-btn").addEventListener("click", () => removeTask(task.id));

    node.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        node.querySelectorAll(".tab-btn").forEach((e) => e.classList.remove("active"));
        node.querySelectorAll("pre.tab").forEach((e) => e.classList.remove("active"));
        btn.classList.add("active");
        const tab = btn.dataset.tab;
        node.querySelector(`pre.${tab}`).classList.add("active");
        if (tab === "transcript") {
          transcriptPre.textContent = await api(`/api/tasks/${task.id}/transcript`).catch((err) => err.message);
        }
        if (tab === "summary") {
          summaryPre.textContent = await api(`/api/tasks/${task.id}/summary`).catch((err) => err.message);
        }
        if (tab === "log") {
          logPre.textContent = await api(`/api/tasks/${task.id}/log`).catch((err) => err.message);
        }
      });
    });

    root._elements = { statusEl, videoProgress, audioProgress, downloadLoading, downloadLoadingText, llamaLoading };
    root._runtime = {
      baseStatus: task.status,
      currentStep,
      llamaStatus: currentStep === "prepare_llama_model" ? "loading" : "idle",
      mediaPhase: ""
    };
    renderTaskRuntime(root);
    taskList.appendChild(node);
  });
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
  await api(`/api/tasks/${taskId}`, { method: "DELETE" });
  await loadTasks();
}

function patchTaskStatus(taskId, status) {
  const taskEl = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  taskEl._runtime.baseStatus = status;
  if (status !== "running") {
    taskEl._runtime.currentStep = "";
    taskEl._runtime.llamaStatus = "idle";
    taskEl._runtime.mediaPhase = "";
  }
  renderTaskRuntime(taskEl);
}

function patchTaskStep(taskId, name, status) {
  const taskEl = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  if (status === "running") {
    taskEl._runtime.currentStep = name;
  } else if (status === "completed" && taskEl._runtime.currentStep === name) {
    taskEl._runtime.currentStep = "";
  } else if (status === "failed") {
    taskEl._runtime.currentStep = `${name} failed`;
  }
  if (name === "prepare_llama_model" && status !== "running") {
    taskEl._runtime.llamaStatus = "idle";
  }
  renderTaskRuntime(taskEl);
}

function patchLlamaModelProgress(taskId, status) {
  const taskEl = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  taskEl._runtime.llamaStatus = status === "loading" ? "loading" : "idle";
  if (status === "failed") {
    taskEl._runtime.currentStep = "prepare_llama_model failed";
  }
  renderTaskRuntime(taskEl);
}

function patchTaskPhase(taskId, phase, status) {
  const taskEl = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!taskEl || !taskEl._elements || !taskEl._runtime) {
    return;
  }
  const phaseName = String(phase || "").toLowerCase();
  if (phaseName === "merge" || phaseName === "postprocess") {
    taskEl._runtime.mediaPhase = status === "running" ? phaseName : "";
    renderTaskRuntime(taskEl);
  }
}

function patchTaskProgress(taskId, video, audio) {
  const taskEl = document.querySelector(`[data-task-id="${taskId}"]`);
  if (!taskEl || !taskEl._elements) {
    return;
  }
  if (typeof video === "number") {
    taskEl._elements.videoProgress.value = video;
  }
  if (typeof audio === "number") {
    taskEl._elements.audioProgress.value = audio;
  }
}

function connectEvents() {
  if (state.eventSource) {
    state.eventSource.close();
  }
  const url = new URL("/api/events", window.location.origin);
  url.searchParams.set("dev_user", state.authUser);
  if (state.actingAs) {
    url.searchParams.set("as_user", state.actingAs);
  }
  state.eventSource = new EventSource(url.toString(), { withCredentials: false });

  state.eventSource.addEventListener("video_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskProgress(payload.task_id, payload.data.progress, null);
  });
  state.eventSource.addEventListener("audio_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskProgress(payload.task_id, null, payload.data.progress);
  });
  state.eventSource.addEventListener("task_status", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStatus(payload.task_id, payload.data.status);
  });
  state.eventSource.addEventListener("step", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskStep(payload.task_id, payload.data.name, payload.data.status);
  });
  state.eventSource.addEventListener("llama_model_progress", (event) => {
    const payload = JSON.parse(event.data);
    patchLlamaModelProgress(payload.task_id, payload.data.status);
  });
  state.eventSource.addEventListener("phase", (event) => {
    const payload = JSON.parse(event.data);
    patchTaskPhase(payload.task_id, payload.data.phase, payload.data.status);
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
}

document.getElementById("refresh-btn").addEventListener("click", loadTasks);
form.addEventListener("submit", createTask);
form.transcript.addEventListener("change", syncSummaryToggle);
adminApplyBtn.addEventListener("click", applyAdminUser);
adminResetBtn.addEventListener("click", resetAdminUser);

setVersionLabel(BUILD_VERSION);
syncSummaryToggle();
refreshAll();

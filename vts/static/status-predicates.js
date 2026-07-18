// Single frontend source of task-status semantics. Pure-status flags come from
// the backend's /api/status-config map (vts.services.task_status.status_flags);
// task-dependent capabilities come from each task's runtime.capabilities.
// No status rule is re-implemented here (vts-c2n).
(function () {
  let FLAGS = {};
  function flag(status, key) {
    const row = FLAGS[String(status || "")];
    return Boolean(row && row[key]);
  }
  window.statusPred = {
    setFlags(map) { FLAGS = map && typeof map === "object" ? map : {}; },
    isActive: (s) => flag(s, "is_active"),
    isPending: (s) => flag(s, "is_pending"),
    isFinished: (s) => flag(s, "is_finished"),
    showsProgress: (s) => flag(s, "shows_progress"),
    canPause: (s) => flag(s, "can_pause"),
    canResume: (s) => flag(s, "can_resume"),
    canArchive: (s) => flag(s, "can_archive"),
    needsInput: (s) => flag(s, "needs_input"),
    canRestartSummary: (rt) => Boolean(rt && rt.capabilities && rt.capabilities.can_restart_summary),
    canRestartFinalSummary: (rt) => Boolean(rt && rt.capabilities && rt.capabilities.can_restart_final_summary),
  };
})();

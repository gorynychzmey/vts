// Verifies the running task-card work-time timer (.task-runtime) sums per-step
// durations and EXCLUDES the idle gap between steps (pause / awaiting input),
// rather than showing the raw span from the first step's start to now.
//
// Setup: a running task with
//   - one completed step that took 30s,
//   - then a ~1 hour idle gap (task was paused between steps),
//   - then a step that started only a few seconds ago and is still running.
// Correct work time ≈ 30s + (now - running_start) ≈ a bit over 30s.
// The old buggy logic (now - firstStepStart) would show ≈ 1 hour (3600s+).
// We assert the displayed timer is far below the span, which discriminates the
// two implementations robustly without needing to control Date.now().
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "task-worktime-excludes-pause";

function isoAgo(secondsAgo) {
  // Build an ISO timestamp `secondsAgo` seconds before now, in UTC.
  return new Date(Date.now() - secondsAgo * 1000).toISOString();
}

// Parse a "MM:SS" or "HH:MM:SS" (or larger) duration label back to seconds.
function parseDurationLabel(text) {
  const parts = text.trim().split(":").map((p) => Number(p));
  if (parts.some((n) => !Number.isFinite(n))) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}

export async function run() {
  const failures = [];

  // First step: started 1h1m ago, finished 1h ago -> 60s of work... wait, keep
  // it a clean 30s of work: started 3630s ago, finished 3600s ago.
  const firstStart = isoAgo(3630); // 1h0m30s ago
  const firstFinish = isoAgo(3600); // 1h ago  -> step took 30s
  // ~1 hour idle gap here (paused between steps).
  const runningStart = isoAgo(5); // running step started 5s ago

  const TASK = {
    id: "77777777-7777-7777-7777-777777777777",
    source_url: "http://x/paused", source_title: "Paused-between-steps",
    status: "running",
    options: { language: null, audio_only: false, transcript: true, prompts: [] },
    steps: [
      { name: "download", status: "completed", started_at: firstStart, finished_at: firstFinish },
      { name: "extract_audio", status: "running", started_at: runningStart, finished_at: null },
    ],
    created_at: firstStart, updated_at: runningStart,
    progress: { transcribe: { current: 0, total: 1 }, summary: { current: 0, total: 0 } },
    stats: {},
  };

  const { server, baseUrl } = await startStubServer({ "/api/tasks": [TASK] });
  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForTimeout(400); // let the timer tick at least once

    const label = await page.evaluate(
      () => document.querySelector(".task .task-runtime")?.textContent || ""
    );
    if (!label) {
      failures.push("running task-card shows no work-time timer (.task-runtime empty)");
      return failures;
    }
    const seconds = parseDurationLabel(label);
    if (seconds === null) {
      failures.push(`work-time timer not a duration: ${JSON.stringify(label)}`);
      return failures;
    }
    // Correct value ≈ 30s + ~5s = ~35s. Buggy span value ≈ 3635s (~1h).
    // Assert it is well under the span (< 300s) AND at least the 30s of
    // completed work (so we didn't accidentally drop the finished step).
    if (seconds >= 300) {
      failures.push(
        `work-time timer counts the between-step pause: showed ${JSON.stringify(label)} ` +
        `(${seconds}s); expected ~35s (30s done + a few seconds of the running step), ` +
        `NOT the ~1h first-to-now span`
      );
    }
    if (seconds < 25) {
      failures.push(
        `work-time timer dropped the finished step: showed ${JSON.stringify(label)} ` +
        `(${seconds}s); expected at least the 30s of completed work`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

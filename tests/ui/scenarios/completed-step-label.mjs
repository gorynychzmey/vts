// Regression (vts-ovn): a completed task must show the LAST step ("N von N"),
// not "1 von N". resolveActiveStep() used to fall through to the download
// heuristic (runtime.download.hasVideo/hasAudio), which stays true after live
// SSE download events were watched during the run. On the post-completion
// re-render that resolved the active step back to "download" => step 1 of N.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "completed-step-label";

// A completed summary task. Its enabled steps are DAG_HEAD + one finalize step
// for system:summary, matching the real summary pipeline.
//
// Keep in sync with vts/pipeline/types.py:DAG_HEAD — this count changes whenever
// a step is added to the pipeline (it went 11 -> 12 when `diarize` landed, so the
// total here went 12 -> 13). A mismatch here means the DAG changed, not that the
// frontend broke.
const EXPECTED_STEPS = 13;
const COMPLETED_TASK = {
  id: "22222222-2222-2222-2222-222222222222",
  source_url: "http://x/v", source_title: "Done video",
  status: "completed", summary_path: "/x/summary/final.md",
  transcript_path: "/x/transcript.txt",
  options: {
    transcript: true,
    prompts: [{ source: "system", id: "summary" }],
    prompt_results: [{ source: "system", id: "summary", name: "Summary", path: "/x", status: "completed" }],
  },
  steps: [
    { name: "download", status: "completed", started_at: "2026-06-28T10:00:00Z", finished_at: "2026-06-28T10:01:00Z" },
    { name: "summarize_final", status: "completed", started_at: "2026-06-28T10:01:00Z", finished_at: "2026-06-28T10:02:00Z" },
  ],
  created_at: "2026-06-28T10:00:00Z", updated_at: "2026-06-28T10:02:00Z",
  progress: { transcribe: { current: 1, total: 1 }, summary: { current: 1, total: 1 } }, stats: {},
};

// Pull "<index> / <total>" out of the step label regardless of locale wording
// ("Schritt N von N", "Шаг N из N", "Step N of N"). The two integers in the
// label are always (index, total).
function parseStepIndex(text) {
  const nums = String(text || "").match(/\d+/g);
  if (!nums || nums.length < 2) return null;
  return { index: Number(nums[0]), total: Number(nums[1]) };
}

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": [COMPLETED_TASK] });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    const taskSel = ".task";
    if (!(await page.$(taskSel))) {
      failures.push("no .task row rendered");
      return failures;
    }

    // Reproduce the real-world trigger: a download flag left set on runtime by
    // live SSE events that were watched while the task was still running. The
    // 1s duration ticker then re-renders the row from this runtime (a periodic
    // loadTasks() poll later rebuilds runtime and clears the flag, so the bug
    // only shows in the first ~2s window — we sample across it).
    await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (el && el._runtime) el._runtime.download.hasVideo = true;
    }, taskSel);

    // Sample the label repeatedly across the vulnerable window. With the bug,
    // a ticker re-render resolves the active step back to "download" => index 1.
    let worstIndex = null;
    let worstLabel = "";
    let total = null;
    for (let i = 0; i < 18; i++) {
      await page.waitForTimeout(100);
      const label = await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        const lbl = el ? el.querySelector(".step-label") : null;
        return lbl ? lbl.textContent : "";
      }, taskSel);
      const parsed = parseStepIndex(label);
      if (!parsed) continue;
      total = parsed.total;
      if (worstIndex === null || parsed.index < worstIndex) {
        worstIndex = parsed.index;
        worstLabel = label;
      }
    }

    if (worstIndex === null) {
      failures.push("could not parse any step label across the sampling window");
    } else {
      if (total !== EXPECTED_STEPS) {
        failures.push(
          `expected ${EXPECTED_STEPS} enabled steps, got total=${total} (label ${JSON.stringify(worstLabel)})`
        );
      }
      if (worstIndex !== total) {
        failures.push(
          `completed task showed step ${worstIndex} of ${total}; expected last step ` +
          `(${total} of ${total}) at all times. Worst label: ${JSON.stringify(worstLabel)}`
        );
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

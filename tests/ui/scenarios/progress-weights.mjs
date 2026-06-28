// Verifies that loadProgressWeights() neither breaks bootstrap (case 1: server
// returns a valid weights payload) nor breaks it on bad data (case 2: server
// returns {}, which leaves serverStepWeights null and triggers the hardcoded
// STEP_WEIGHT_SECONDS fallback).  The harness always responds HTTP 200 for
// GET /api/*, so a forced-500 is impossible; {} is used instead of a 500 to
// exercise the fallback guard: `data && data.weights && typeof data.weights === "object"`.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "progress-weights";

export async function run() {
  const failures = [];

  // Case 1: endpoint returns server weights -> app boots normally.
  {
    const { server, baseUrl } = await startStubServer({
      "/api/progress-weights": {
        weights: {
          download: 5.5,
          extract_audio: 2.0,
          trim_initial_silence: 0.3,
          segment_audio: 1.2,
          detect_language: 2.6,
          transcribe_segments: 174.8,
          merge_transcript: 0.1,
          prepare_llama_model: 6.3,
          prepare_summary_chunks: 0.1,
          summarize_windows: 74.8,
        },
        final_summary_fallback: 514.4,
      },
    });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      if (!(await isVisible(page, "#app-version"))) {
        failures.push("case1: app did not boot (version label missing)");
      }
      if (errors.length) failures.push(`case1: console errors: ${errors.join("; ")}`);
    } finally {
      await browser.close();
      server.close();
    }
  }

  // Case 2: endpoint returns {} (no usable weights) -> harness can't force a
  // non-200 response, so {} simulates an "empty/garbage" payload.  The client
  // guard leaves serverStepWeights null and falls back to hardcoded constants;
  // the app must still boot.
  {
    const { server, baseUrl } = await startStubServer({
      "/api/progress-weights": {},
    });
    const browser = await launch();
    try {
      const { page, errors } = await openPage(browser, baseUrl);
      if (!(await isVisible(page, "#app-version"))) {
        failures.push("case2: app did not boot on empty weights payload (fallback broken)");
      }
      if (errors.length) failures.push(`case2: console errors: ${errors.join("; ")}`);
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}

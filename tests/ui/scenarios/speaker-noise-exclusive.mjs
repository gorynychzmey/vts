// Noise and a person binding are mutually exclusive.
//
// "This is not a person" and "this is Anna" cannot both be true, but nothing
// enforced that: the checkbox and the selection were independent, so a row
// could carry both and send a contradictory resolution to the backend.
//
// Asserted from BOTH surfaces, because they reach the state by different paths:
// the panel writes through bindSpeakerRow, the dialog through onVoiceRowRebind.
import { startStubServer, launch, openPage, clickReal } from "../harness.mjs";

export const name = "speaker-noise-exclusive";

const iso = new Date().toISOString();
const FLAGS = {
  awaiting_input: {
    is_active: false, is_pending: false, is_finished: false, shows_progress: false,
    can_pause: false, can_resume: true, can_archive: true, needs_input: true,
  },
};
const TASK = {
  id: "a", status: "awaiting_input", awaiting_step: "match_speakers",
  source_url: "https://y/a", source_title: "T", display_name: "T",
  created_at: iso, updated_at: iso, media_path: "/m.mp4",
  capabilities: {}, options: { transcript: true, diarize: true, prompts: [] }, steps: [],
};
const SPEAKERS = [{ id: "s1", name: "Anna" }];

export async function run() {
  const failures = [];

  // --- panel: bind a person, then mark noise -> the binding must be dropped ---
  {
    const matches = {
      SPEAKER_00: {
        display_label: "Voice 1", outcome: "grey", share: 0.5, seconds: 60,
        candidates: [{ speaker_id: "s1", name: "Anna", distance: 0.12 }],
      },
    };
    const { server, baseUrl } = await startStubServer({
      "/api/status-config": { status_flags: FLAGS }, "/api/tasks": [TASK],
      "/api/tasks/a/speaker-matches": matches, "/api/speakers": SPEAKERS,
    });
    const browser = await launch();
    try {
      const { page } = await openPage(browser, baseUrl, { width: 1100, height: 1000 });
      const posts = [];
      page.on("request", (r) => {
        if (r.method() === "POST" && r.url().includes("/speakers")) posts.push(JSON.parse(r.postData()));
      });
      await clickReal(page, ".toggle-btn");
      await page.waitForTimeout(900);

      await clickReal(page, ".speaker-box-list .speaker-chip");
      await page.waitForTimeout(600);
      const bound = posts[posts.length - 1]?.resolutions?.[0];
      if (bound?.action !== "bind_existing" || bound?.speaker_id !== "s1") {
        failures.push(`panel: chip should bind the person, sent ${JSON.stringify(bound)}`);
      }
      if (bound?.is_noise !== false) failures.push(`panel: a fresh binding must not be noise, sent ${JSON.stringify(bound)}`);

      await clickReal(page, ".speaker-box-list .voice-row-noise-toggle input");
      await page.waitForTimeout(600);
      const noised = posts[posts.length - 1]?.resolutions?.[0];
      if (noised?.is_noise !== true) failures.push(`panel: noise flag did not reach the backend, sent ${JSON.stringify(noised)}`);
      // The point of the fix: the person binding is gone, not carried alongside.
      if (noised?.action !== "leave_anonymous" || noised?.speaker_id) {
        failures.push(`panel: marking noise must drop the binding, sent ${JSON.stringify(noised)}`);
      }
    } finally {
      await browser.close();
      server.close();
    }
  }

  // --- dialog: a noisy row, then pick a person -> the noise flag must clear ---
  {
    const matches = {
      SPEAKER_00: {
        display_label: "Voice 1", outcome: "grey", share: 0.5, seconds: 60, noise: true,
        candidates: [{ speaker_id: "s1", name: "Anna", distance: 0.12 }],
      },
    };
    const { server, baseUrl } = await startStubServer({
      "/api/status-config": { status_flags: FLAGS }, "/api/tasks": [TASK],
      "/api/tasks/a/speaker-matches": matches, "/api/speakers": SPEAKERS,
    });
    const browser = await launch();
    try {
      const { page } = await openPage(browser, baseUrl, { width: 1100, height: 1100 });
      await clickReal(page, ".task-menu-btn");
      await page.waitForTimeout(250);
      await clickReal(page, ".resolve-voices-btn");
      await page.waitForTimeout(700);

      const before = await page.evaluate(() => ({
        noise: document.querySelector("#voice-list .voice-row-noise-toggle input")?.checked,
      }));
      if (before.noise !== true) {
        failures.push(`dialog: fixture says noise:true but the box is ${JSON.stringify(before.noise)}`);
      }

      await page.selectOption("#voice-list .voice-select", "s1");
      await page.waitForTimeout(300);
      const after = await page.evaluate(() => ({
        noise: document.querySelector("#voice-list .voice-row-noise-toggle input")?.checked,
        select: document.querySelector("#voice-list .voice-select")?.value,
        dimmed: document.querySelector("#voice-list .voice-row")?.classList.contains("voice-row-noise"),
      }));
      if (after.select !== "s1") failures.push(`dialog: selection did not take, got ${JSON.stringify(after.select)}`);
      if (after.noise !== false) failures.push("dialog: picking a person must clear the noise checkbox");
      if (after.dimmed !== false) failures.push("dialog: the row must stop being dimmed once a person is bound");
    } finally {
      await browser.close();
      server.close();
    }
  }

  return failures;
}

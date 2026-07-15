// Regression (vts-86k, VOS-91): "Audio only" and the File source.
//
// audio_only is a yt-dlp download hint -- its only consumer is
// services/downloader.py:306, and DownloadStep returns early for file:// tasks
// (pipeline/steps/media.py:58-62), so the flag is meaningless for an upload.
//
// The control is therefore HIDDEN for the File source rather than cleared.
// Clearing .checked was itself a data-loss bug: currentFormOptions() reads
// .checked and optionsEqual() compares it, so a cleared box made an applied
// preset look dirty -> "Save changes" -> a PATCH would overwrite the stored
// preset's audio_only:true with false, permanently. The flag is dropped at the
// upload boundary instead (app.js: fields.audio_only = false), so the form
// keeps the user's choice while the server never sees a meaningless true.
//
// Pins: (1) pill hidden on File, .checked NOT forced false; (2) an applied
// preset stays CLEAN on File -- the data-loss regression; (3) URL unaffected;
// (4) URL->File->URL round-trip; (5) the boundary strip, observed on the real
// outgoing multipart request.
import { startStubServer, launch, openPage, isVisible } from "../harness.mjs";

export const name = "audio-only-file-source";

// Real shapes: "summary" is the only system prompt (prompt_registry.py:16-17);
// "memo" is a user prompt. The user preset carries audio_only:true and is
// editable -- required for the dirty/"Save changes" path to be reachable.
const PROMPTS = [
  { source: "system", id: "summary", name: "Summary", editable: false },
  { source: "user", id: "u1", name: "Memo", editable: true },
];
const PRESETS = [
  {
    source: "system",
    id: "default",
    name: "Default",
    editable: false,
    options: {
      language: "",
      audio_only: false,
      transcript: true,
      prompts: [{ source: "system", id: "summary" }],
    },
  },
  {
    source: "user",
    id: "p1",
    name: "Audio memo",
    editable: true,
    options: {
      language: "ru",
      audio_only: true,
      transcript: true,
      prompts: [
        { source: "system", id: "summary" },
        { source: "user", id: "u1" },
      ],
    },
  },
];

// The source-type radios are visually hidden and driven by their labels.
const pickSource = (page, which) => page.click(`label:has(#source-type-${which})`);

const audioOnly = (page) =>
  page.evaluate(() => {
    const el = document.getElementById("audio_only");
    return { checked: el.checked, disabled: el.disabled };
  });

const presetSaveBtn = (page) =>
  page.evaluate(() => {
    const b = document.getElementById("preset-save-btn");
    return { text: b.textContent.trim(), mode: b.dataset.mode };
  });

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/prompts": PROMPTS,
    "/api/presets": PRESETS,
    "/api/me/default_preset": { source: "system", id: "default" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // --- 3. URL source: pill visible, preset still sets the flag. ---
    await page.selectOption("#preset-select", "user:p1");
    await page.waitForTimeout(150);
    const onUrl = await audioOnly(page);
    if (!onUrl.checked) failures.push("url: preset must set audio_only checked");
    if (onUrl.disabled) failures.push("url: audio_only must not be disabled");
    if (!(await isVisible(page, "#audio-only-pill"))) {
      failures.push("url: audio-only pill must be visible");
    }

    // --- 1. File source: pill hidden, .checked preserved (NOT forced false). ---
    await pickSource(page, "file");
    await page.waitForTimeout(150);
    if (await isVisible(page, "#audio-only-pill")) {
      failures.push("file: audio-only pill must be hidden");
    }
    const onFile = await audioOnly(page);
    if (!onFile.checked) {
      failures.push("file: .checked must be preserved (not forced false) while hidden");
    }

    // --- 2. THE DATA-LOSS REGRESSION: the applied preset must stay CLEAN. ---
    // Re-apply while already on File, then trigger recomputePresetDirty via an
    // unrelated control. If audio_only were being cleared, currentFormOptions()
    // would no longer match the preset -> button flips to patch/"Save changes"
    // -> saving would overwrite the stored preset's audio_only:true with false.
    await page.selectOption("#preset-select", "system:default");
    await page.waitForTimeout(150);
    await page.selectOption("#preset-select", "user:p1");
    await page.waitForTimeout(150);
    const reapplied = await audioOnly(page);
    if (!reapplied.checked) {
      failures.push("file: preset applied on File must keep audio_only checked");
    }
    const cleanAfterApply = await presetSaveBtn(page);
    if (cleanAfterApply.mode === "patch") {
      failures.push(
        `file: freshly applied preset must be clean, got mode=${cleanAfterApply.mode}`
      );
    }
    // Toggle transcript twice: back to the preset's value, so options match
    // again. Any dirtiness left now comes from audio_only, not from transcript.
    await page.click("#transcript");
    await page.waitForTimeout(100);
    await page.click("#transcript");
    await page.waitForTimeout(150);
    const afterToggle = await presetSaveBtn(page);
    if (afterToggle.mode === "patch" || afterToggle.text === "Save changes") {
      failures.push(
        `file: preset went DIRTY (data-loss bug: PATCH would clear stored audio_only) ` +
          `-- save btn text="${afterToggle.text}" mode=${afterToggle.mode}`
      );
    }

    // --- 4. Round-trip URL -> File -> URL keeps the value and restores the pill. ---
    await pickSource(page, "url");
    await page.waitForTimeout(150);
    const backToUrl = await audioOnly(page);
    if (!backToUrl.checked) failures.push("file->url: audio_only must still be checked");
    if (!(await isVisible(page, "#audio-only-pill"))) {
      failures.push("file->url: pill must be visible again");
    }

    // --- 5. THE BOUNDARY STRIP: submit a file upload with audio_only checked
    // and assert the REAL outgoing multipart carries audio_only=false. ---
    await pickSource(page, "file");
    await page.waitForTimeout(150);
    if (!(await audioOnly(page)).checked) {
      failures.push("pre-submit: expected audio_only checked (setup for boundary check)");
    }

    let sentAudioOnly = null;
    await page.route("**/api/tasks/upload", async (route) => {
      // postData() gives the raw multipart body; read the audio_only part.
      const body = route.request().postData() || "";
      const m = body.match(/name="audio_only"\r?\n\r?\n([^\r\n]*)/);
      sentAudioOnly = m ? m[1].trim() : `(not found in body: ${body.slice(0, 200)})`;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "t1", status: "queued" }),
      });
    });

    await page.setInputFiles("#file-input", {
      name: "memo.mp3",
      mimeType: "audio/mpeg",
      buffer: Buffer.from("fake audio bytes"),
    });
    await page.click("#submit-btn");
    await page.waitForTimeout(600);

    if (sentAudioOnly === null) {
      failures.push("boundary: upload request was never sent -- could not observe audio_only");
    } else if (sentAudioOnly !== "false") {
      failures.push(
        `boundary: upload must send audio_only=false, got "${sentAudioOnly}" ` +
          `(a meaningless yt-dlp flag reached the server)`
      );
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Regression (vts-86k, VOS-91): a preset must not set "Audio only" for the
// File source. audio_only is a yt-dlp download hint (services/downloader.py:306)
// and DownloadStep skips yt-dlp entirely for file:// tasks
// (pipeline/steps/media.py:58-62), so syncSourceType() disables+clears the
// checkbox for File. applyPresetOptions() used to write form.audio_only.checked
// straight from the preset without re-running that gate, so a preset could
// leave a *disabled* box checked -- and the upload path reads it verbatim
// (app.js:2471) and submits audio_only=true the user could neither see nor undo.
//
// Both directions are asserted: File must clear+disable it, URL must still let
// the preset set it (the flag is legitimate for download tasks).
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "preset-audio-only-file-source";

// Mirrors the real shapes: "summary" is the only system prompt
// (services/prompt_registry.py:16-17); "memo" is a user prompt. The user preset
// carries audio_only:true plus summary+memo -- the combination from the ticket.
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

    // --- Direction 1: URL source -- the preset MUST still set audio_only. ---
    await page.selectOption("#preset-select", "user:p1");
    await page.waitForTimeout(150);
    const url = await audioOnly(page);
    if (!url.checked) failures.push("url: preset should set audio_only checked");
    if (url.disabled) failures.push("url: audio_only should stay enabled");

    // --- Direction 2 (the regression): switch to File with the preset applied. ---
    await pickSource(page, "file");
    await page.waitForTimeout(150);
    const afterFile = await audioOnly(page);
    if (afterFile.checked) failures.push("file: audio_only must be cleared when switching to File");
    if (!afterFile.disabled) failures.push("file: audio_only must be disabled for File source");

    // --- Direction 2b: apply the preset while ALREADY on File. This is the
    // exact path the fix closes -- applyPresetOptions must re-run the gate. ---
    await page.selectOption("#preset-select", "system:default");
    await page.waitForTimeout(150);
    await page.selectOption("#preset-select", "user:p1");
    await page.waitForTimeout(150);
    const presetOnFile = await audioOnly(page);
    if (presetOnFile.checked) {
      failures.push("file: preset must not set audio_only checked on File source");
    }
    if (!presetOnFile.disabled) {
      failures.push("file: audio_only must remain disabled after applying preset on File");
    }

    // --- Direction 3: File -> URL -> File stays consistent. ---
    await pickSource(page, "url");
    await page.waitForTimeout(150);
    const backToUrl = await audioOnly(page);
    if (backToUrl.disabled) failures.push("file->url: audio_only should be re-enabled");

    await pickSource(page, "file");
    await page.waitForTimeout(150);
    const backToFile = await audioOnly(page);
    if (backToFile.checked) failures.push("url->file: audio_only must be cleared again");
    if (!backToFile.disabled) failures.push("url->file: audio_only must be disabled again");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

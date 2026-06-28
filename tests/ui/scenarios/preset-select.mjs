// Verifies the create-form preset dropdown: it renders options from
// /api/presets, the default preset (/api/me/default_preset) is applied to the
// form on load, and selecting a different preset re-applies its options.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "preset-select";

export async function run() {
  // System "default" (transcript on, no audio_only) selected as the user's
  // default; one user preset "Audio memo" (audio_only on, transcript off).
  const { server, baseUrl } = await startStubServer({
    "/api/presets": [
      {
        source: "system",
        id: "default",
        name: "Default",
        editable: false,
        options: { language: "", audio_only: false, transcript: true, prompts: [] },
      },
      {
        source: "user",
        id: "p1",
        name: "Audio memo",
        editable: true,
        options: {
          language: "ru",
          audio_only: true,
          transcript: false,
          prompts: [{ source: "user", id: "u1" }],
        },
      },
    ],
    "/api/me/default_preset": { source: "system", id: "default" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // Dropdown renders with both options.
    const optCount = await page.$$eval("#preset-select option", (els) => els.length);
    if (optCount !== 2) {
      failures.push(`expected 2 preset options, got ${optCount}`);
    }
    const selectedValue = await page.$eval("#preset-select", (el) => el.value);
    if (selectedValue !== "system:default") {
      failures.push(`default preset not selected (value=${selectedValue})`);
    }

    // Default preset options applied: transcript checked, audio_only not.
    const afterDefault = await page.evaluate(() => ({
      transcript: document.getElementById("transcript").checked,
      audio_only: document.getElementById("audio_only").checked,
      language: document.getElementById("language").value,
      saveLabel: document.getElementById("preset-save-btn").textContent,
    }));
    if (!afterDefault.transcript) failures.push("default: transcript should be checked");
    if (afterDefault.audio_only) failures.push("default: audio_only should be unchecked");
    if (afterDefault.language !== "") failures.push(`default: language should be empty, got ${afterDefault.language}`);

    // Select the user preset -> options re-apply.
    await page.selectOption("#preset-select", "user:p1");
    await page.waitForTimeout(150);
    const afterUser = await page.evaluate(() => ({
      transcript: document.getElementById("transcript").checked,
      audio_only: document.getElementById("audio_only").checked,
      language: document.getElementById("language").value,
      value: document.getElementById("preset-select").value,
    }));
    if (afterUser.value !== "user:p1") failures.push("user preset not selected after change");
    if (afterUser.transcript) failures.push("user: transcript should be unchecked");
    if (!afterUser.audio_only) failures.push("user: audio_only should be checked");
    if (afterUser.language !== "ru") failures.push(`user: language should be ru, got ${afterUser.language}`);

    // Toggling a control makes the preset dirty -> button switches to "save changes".
    await page.click("#transcript");
    await page.waitForTimeout(100);
    const dirtyLabel = await page.$eval("#preset-save-btn", (el) => el.textContent.trim());
    if (dirtyLabel !== "Save changes") {
      failures.push(`dirty user preset save label should be "Save changes", got "${dirtyLabel}"`);
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

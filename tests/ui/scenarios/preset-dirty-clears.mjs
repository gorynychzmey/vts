// Regression (vts-oz84): the "changed" badge never went out again.
//
// applyPresetOptions() does not put the preset in the form verbatim — it drops
// prompt refs whose prompt no longer exists (filterDanglingPrompts) and
// delivery refs whose target is gone. recomputePresetDirty() compared the form
// against the RAW preset.options, so for any preset carrying a stale ref the
// two could never be equal. Applying the preset hid that (applyPresetById
// forces presetDirty = false), but after touching any control the badge latched
// on: putting every value back still did not match the raw options.
//
// The preset below deliberately references a prompt that /api/prompts does not
// return — exactly the state a preset reaches when a prompt it named is
// deleted, which is how this reached a real user.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "preset-dirty-clears";

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/presets": [
      {
        source: "user",
        id: "p1",
        name: "Weekly sync",
        editable: true,
        options: {
          language: "ru",
          audio_only: false,
          transcript: true,
          diarize: false,
          prompts: [
            { source: "user", id: "u1" },        // exists in DEFAULT_API
            { source: "user", id: "deleted-99" }, // dangling: filtered on apply
          ],
        },
      },
    ],
    "/api/me/default_preset": { source: "user", id: "p1" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    const badgeHidden = () =>
      page.$eval("#preset-dirty-badge", (el) => el.classList.contains("hidden"));

    // Freshly applied: clean, whatever the dangling ref did.
    if (!(await badgeHidden())) {
      failures.push("'changed' badge is showing on a freshly applied preset");
    }

    // Change something -> badge appears.
    await page.click("#audio_only");
    await page.waitForTimeout(120);
    if (await badgeHidden()) {
      failures.push("'changed' badge did not appear after toggling audio_only");
      return failures;
    }

    // Put it back -> the badge must go out again. This is the actual bug: the
    // form is byte-for-byte where it started, and the badge stayed lit.
    await page.click("#audio_only");
    await page.waitForTimeout(120);
    if (!(await badgeHidden())) {
      failures.push(
        "'changed' badge still showing after returning every control to its " +
        "original value — the dirty check compares against the unfiltered preset, " +
        "so a preset with a deleted prompt ref can never read as clean again"
      );
    }

    // And the same for a second, independent control, so the fix is not a
    // one-field special case.
    await page.click("#transcript");
    await page.waitForTimeout(120);
    if (await badgeHidden()) {
      failures.push("'changed' badge did not appear after toggling transcript");
    }
    await page.click("#transcript");
    await page.waitForTimeout(120);
    if (!(await badgeHidden())) {
      failures.push("'changed' badge still showing after restoring transcript");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

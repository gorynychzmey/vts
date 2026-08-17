// Redesign v2, stage 4: the staged multi-file selection.
//
// Before this, `multiple` worked and submit read fileInput.files, but nothing
// rendered the selection: the user could not see what was picked, could not
// remove one file, and a second pick REPLACED the set. FileList is read-only,
// so the fix moves the source of truth into a File[] and rebuilds the input
// from it — these assertions exist to prove the input really does mirror the
// array, because the whole submit path still reads through it.
import { startStubServer, launch, openPage, isVisible, clickReal } from "../harness.mjs";

export const name = "file-staging";

// Pick files through the real input: setInputFiles is the only way to hand a
// browser a File without a user gesture, but everything after it is real.
async function pick(page, files) {
  await page.setInputFiles("#file-input", files);
  await page.waitForTimeout(150);
}

const rows = (page) =>
  page.evaluate(() =>
    [...document.querySelectorAll("#file-list .file-row")].map((r) => ({
      num: r.querySelector(".file-row-num")?.textContent?.trim(),
      name: r.querySelector(".file-row-name")?.textContent?.trim(),
    })),
  );

// What the form would actually submit — the mirrored FileList, not the array.
const inputNames = (page) =>
  page.evaluate(() => [...(document.getElementById("file-input").files || [])].map((f) => f.name));

export async function run() {
  const { server, baseUrl } = await startStubServer();
  const browser = await launch();
  const failures = [];
  const mk = (name, body) => ({ name, mimeType: "video/mp4", buffer: Buffer.from(body) });
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    if (errors.length) failures.push("JS errors on boot: " + JSON.stringify(errors));

    // The zone belongs to the File source only.
    if (await isVisible(page, "#file-drop"))
      failures.push("#file-drop visible while the URL source is selected");
    await clickReal(page, "label:has(#source-type-file)");
    await page.waitForTimeout(150);
    if (!(await isVisible(page, "#file-drop"))) failures.push("#file-drop hidden for the File source");
    if (!(await isVisible(page, "#file-drop-empty"))) failures.push("empty state not shown with no files");

    // --- picking renders rows ---
    await pick(page, [mk("a.mp4", "aaa"), mk("b.mp4", "bb")]);
    let r = await rows(page);
    if (r.length !== 2) failures.push(`expected 2 rows, got ${r.length}`);
    if (r.map((x) => x.name).join() !== "a.mp4,b.mp4") failures.push(`rows = ${JSON.stringify(r)}`);
    if (r.map((x) => x.num).join() !== "1,2") failures.push(`numbering = ${r.map((x) => x.num).join()}`);
    if (await isVisible(page, "#file-drop-empty")) failures.push("empty state still shown with files staged");

    // --- a second pick ADDS rather than replaces (the old bug) ---
    await pick(page, [mk("c.mp4", "c")]);
    r = await rows(page);
    if (r.map((x) => x.name).join() !== "a.mp4,b.mp4,c.mp4")
      failures.push(`second pick should append, got ${r.map((x) => x.name).join()}`);

    // --- duplicates are skipped AND reported ---
    await pick(page, [mk("b.mp4", "bb")]);
    r = await rows(page);
    if (r.length !== 3) failures.push(`duplicate should not be added, got ${r.length} rows`);
    if (!(await isVisible(page, "#file-warning"))) failures.push("duplicate skipped without telling the user");
    const warn = await page.evaluate(() => document.getElementById("file-warning")?.textContent || "");
    if (!warn.includes("b.mp4")) failures.push(`duplicate warning does not name the file: "${warn}"`);

    // Same name, different size is a different file — must NOT be treated as a dup.
    await pick(page, [mk("b.mp4", "bbbbbbbb")]);
    r = await rows(page);
    if (r.length !== 4) failures.push(`same name + different size should be added, got ${r.length} rows`);

    // --- removing one row keeps the rest, and renumbers ---
    await page.click("#file-list .file-row:nth-child(2) .file-row-remove");
    await page.waitForTimeout(150);
    r = await rows(page);
    if (r.map((x) => x.name).join() !== "a.mp4,c.mp4,b.mp4")
      failures.push(`after removing row 2: ${r.map((x) => x.name).join()}`);
    if (r.map((x) => x.num).join() !== "1,2,3") failures.push(`numbering not rebuilt: ${r.map((x) => x.num).join()}`);

    // --- order is user-controlled (keyboard path; drag is mouse-only) ---
    await page.click("#file-list .file-row:nth-child(3) .file-row-move:not([disabled])");
    await page.waitForTimeout(150);
    r = await rows(page);
    if (r.map((x) => x.name).join() !== "a.mp4,b.mp4,c.mp4")
      failures.push(`move up failed: ${r.map((x) => x.name).join()}`);

    // --- the mirrored input matches the array, in order ---
    // This is what submit actually reads, so the order shown must be the order sent.
    const mirrored = await inputNames(page);
    if (mirrored.join() !== "a.mp4,b.mp4,c.mp4")
      failures.push(`input mirror out of step with the rows: ${mirrored.join()}`);

    // First row cannot move up; last cannot move down.
    const edges = await page.evaluate(() => {
      const rs = [...document.querySelectorAll("#file-list .file-row")];
      const mv = (i) => [...rs[i].querySelectorAll(".file-row-move")].map((b) => b.disabled);
      return { first: mv(0), last: mv(rs.length - 1) };
    });
    if (edges.first[0] !== true) failures.push("first row's move-up is enabled");
    if (edges.last[1] !== true) failures.push("last row's move-down is enabled");
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

// Verifies the prompt editor's delete button doing both of its jobs (vts-kujy).
//
// Each user now owns an editable copy of the vendor prompt as an ordinary row
// flagged `is_system`. Restoring the vendor text IS deleting that row — the next
// request recreates it from the file — so the same button, in the same place,
// must read "Restore" for it and "Delete" for an ordinary user prompt. Both ask
// first: deleting a user prompt used to be irreversible AND silent.
//
// The fourth assertion is the one that catches a real trap rather than a
// hypothetical one. The button carries a STATIC data-i18n attribute, and
// applyI18n() rewrites textContent from that attribute for every element that
// has it, on every language switch. A label set only imperatively would revert
// to "Delete" while the system prompt is still open, leaving a button that says
// Delete but restores. The fix is to move the state INTO the attribute; this
// scenario switches the locale with the system prompt open and proves the label
// survives.
import { startStubServer, launch, openPage, dialogOpen, clickReal, settled, openFromHeaderMenu } from "../harness.mjs";

export const name = "system-prompt-restore";

const SYSTEM_ID = "11111111-1111-1111-1111-111111111111";
const USER_ID = "22222222-2222-2222-2222-222222222222";
// The restore DELETEs the row and the next read recreates it — with a new id.
const RESTORED_ID = "33333333-3333-3333-3333-333333333333";

const PROMPTS = [
  { source: "user", id: SYSTEM_ID, name: "My tweaked summary", editable: true, is_system: true },
  { source: "user", id: USER_ID, name: "Meeting memo", editable: true, is_system: false },
];

const label = (page) => page.textContent("#prompt-delete-btn").then((s) => (s || "").trim());

// Selecting a row loads it through `await api(...)`, so the editor is filled a
// tick AFTER the click. settled() cannot see that: it fingerprints geometry,
// and relabelling a button barely moves it — two frames match while the fetch
// is still in flight. Wait for the id the editor actually holds instead.
async function openRow(page, id) {
  await clickReal(page, `#prompts-list .mgr-item[data-prompt-id="${id}"]`);
  await page.waitForFunction(
    (want) => document.getElementById("prompt-edit-id")?.value === want,
    id
  );
  await settled(page);
}

// The locale entry keeps the menu open by design, so a blind click on the burger
// would CLOSE it and the next locale click would miss.
async function openHeaderMenu(page) {
  const open = await page.$eval("#header-menu", (el) => el.classList.contains("open")).catch(() => false);
  if (!open) {
    await clickReal(page, "#header-menu-btn");
    await page.waitForSelector("#locale-toggle-btn", { state: "visible" });
    await settled(page);
  }
}

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/prompts": PROMPTS,
    [`/api/prompts/${SYSTEM_ID}`]: {
      source: "user",
      id: SYSTEM_ID,
      name: "My tweaked summary",
      system_prompt: "EDITED vendor text",
      editable: true,
    },
    // The row the restore recreates: same flag, vendor name, pristine text.
    [`/api/prompts/${RESTORED_ID}`]: {
      source: "user",
      id: RESTORED_ID,
      name: "Summary",
      system_prompt: "PRISTINE vendor text",
      editable: true,
      is_system: true,
    },
    [`/api/prompts/${USER_ID}`]: {
      source: "user",
      id: USER_ID,
      name: "Meeting memo",
      system_prompt: "user text",
      editable: true,
    },
    "/api/prompts/system/summary/text": { system_prompt: "PRISTINE vendor text" },
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);

    // Record every request the page makes, so "sent no DELETE" is an assertion
    // about the wire rather than about the UI's mood afterwards.
    const seen = [];
    page.on("request", (req) => seen.push(`${req.method()} ${new URL(req.url()).pathname}`));
    const since = () => seen.length;
    const sentSince = (mark) => seen.slice(mark);

    await openFromHeaderMenu(page, "#prompts-btn");
    if (!(await dialogOpen(page, "prompts-dialog"))) {
      failures.push("prompts-dialog did not open");
      return failures;
    }

    const rows = await page.$$eval("#prompts-list .mgr-item", (els) => els.map((el) => el.dataset.promptId));
    if (rows.length !== 2) failures.push(`expected 2 prompt rows, got ${rows.length}`);

    // --- (0) An ordinary user prompt still reads "Delete" ---
    await openRow(page, USER_ID);
    const userLabel = await label(page);
    if (userLabel !== "Delete prompt") {
      failures.push(`user prompt: button reads ${JSON.stringify(userLabel)}, expected "Delete prompt"`);
    }

    // --- (1) The system prompt reads the RESTORE label, not the delete one ---
    await openRow(page, SYSTEM_ID);
    const sysLabel = await label(page);
    if (sysLabel !== "Restore") {
      failures.push(`system prompt: button reads ${JSON.stringify(sysLabel)}, expected "Restore"`);
    }
    // It must still be the visible, usable button — not merely relabelled.
    const hidden = await page.$eval("#prompt-delete-btn", (el) => el.classList.contains("hidden"));
    if (hidden) failures.push("the restore button is hidden for the system prompt");

    // --- (4) THE TRAP: the label must survive a language switch ---
    // applyI18n() rewrites textContent from data-i18n on every locale change.
    // If the label were set imperatively only, this reverts it to "Delete".
    // Drive the app's own language control rather than a synthetic re-apply, so
    // this exercises what a user actually does. The prompts dialog is modal, so
    // the header menu is out of reach while it is open: close it, switch, and
    // re-open WITHOUT re-picking the row — the button must still be correct for
    // what is still loaded in the editor.
    await page.evaluate(() => document.getElementById("prompts-dialog")?.close());
    await settled(page);
    await openHeaderMenu(page);
    await clickReal(page, "#locale-toggle-btn"); // en -> ru
    await page.waitForFunction(() => document.documentElement.lang === "ru");
    await settled(page);
    await page.evaluate(() => document.getElementById("prompts-dialog")?.showModal());
    await settled(page);
    const afterSwitch = await label(page);
    // Russian, because the point is that it re-resolved through the CORRECT key
    // rather than being left alone or reverted to the delete wording.
    if (afterSwitch !== "Восстановить") {
      failures.push(
        `after a locale switch the button reads ${JSON.stringify(afterSwitch)}, expected "Восстановить" — ` +
          `applyI18n resolved the wrong data-i18n key (the label reverted to Delete)`
      );
    }
    const keyAfterSwitch = await page.$eval("#prompt-delete-btn", (el) => el.getAttribute("data-i18n"));
    if (keyAfterSwitch !== "prompts.manage.restore") {
      failures.push(`data-i18n after locale switch = ${JSON.stringify(keyAfterSwitch)}, expected "prompts.manage.restore"`);
    }
    // Back to English so the remaining assertions read in one language: ru -> de -> en.
    await page.evaluate(() => document.getElementById("prompts-dialog")?.close());
    await settled(page);
    for (const want of ["de", "en"]) {
      // The locale entry deliberately keeps the menu open (cycling three states
      // should not mean reopening it twice), so only open it when it is closed.
      await openHeaderMenu(page);
      await clickReal(page, "#locale-toggle-btn");
      await page.waitForFunction((l) => document.documentElement.lang === l, want);
      await settled(page);
    }
    await page.evaluate(() => document.getElementById("prompts-dialog")?.showModal());
    await settled(page);
    if ((await label(page)) !== "Restore") {
      failures.push("the restore label did not come back in English after switching locales back");
    }

    // --- (2) Dismissing the confirmation sends NO DELETE ---
    let dismissedMsg = "";
    page.once("dialog", async (d) => {
      dismissedMsg = d.message();
      await d.dismiss();
    });
    let mark = since();
    await clickReal(page, "#prompt-delete-btn");
    await page.waitForTimeout(300);
    if (!dismissedMsg) failures.push("restore did not ask for confirmation");
    const afterDismiss = sentSince(mark).filter((r) => r.startsWith("DELETE"));
    if (afterDismiss.length) {
      failures.push(`dismissing the confirm still sent ${JSON.stringify(afterDismiss)}`);
    }

    // --- (3) Accepting it sends DELETE, then GETs the restored vendor text ---
    page.once("dialog", async (d) => {
      await d.accept();
    });
    // The backend recreates the row lazily on the next read: a NEW id, and the
    // vendor's own name back in place of whatever the user had renamed it to.
    // The stub serialises this array on every request, so mutating it here is
    // what the next GET /api/prompts will see.
    PROMPTS[0] = { source: "user", id: RESTORED_ID, name: "Summary", editable: true, is_system: true };
    mark = since();
    await clickReal(page, "#prompt-delete-btn");
    await page.waitForTimeout(600);
    const after = sentSince(mark);
    const del = after.findIndex((r) => r === `DELETE /api/prompts/${SYSTEM_ID}`);
    const get = after.findIndex((r) => r === "GET /api/prompts/system/summary/text");
    if (del < 0) failures.push(`accepting the confirm did not DELETE the row; saw ${JSON.stringify(after)}`);
    if (get < 0) failures.push(`the restored text was not fetched; saw ${JSON.stringify(after)}`);
    if (del >= 0 && get >= 0 && get < del) {
      failures.push("fetched the restored text BEFORE deleting the row");
    }
    // The restored vendor text must be on screen, not the edited copy.
    const body = await page.inputValue("#prompt-body-input");
    if (body !== "PRISTINE vendor text") {
      failures.push(`after restore the editor shows ${JSON.stringify(body)}, expected the vendor text`);
    }

    // --- (5) Restoring returns the NAME too, not just the body ---
    // The row was recreated from the vendor file, so the list shows the vendor
    // name. If the field kept the user's edited name the two panes would
    // disagree, and Save would PATCH that name right back onto the row the user
    // just reset — a half-restore that silently undoes itself.
    const restoredName = await page.inputValue("#prompt-name-input");
    if (restoredName !== "Summary") {
      failures.push(`after restore the name field reads ${JSON.stringify(restoredName)}, expected the vendor name "Summary"`);
    }
    // And the editor must point at the RECREATED row, not the deleted id.
    const editIdAfter = await page.inputValue("#prompt-edit-id");
    if (editIdAfter !== RESTORED_ID) {
      failures.push(`after restore the editor holds id ${JSON.stringify(editIdAfter)}, expected the recreated row ${RESTORED_ID}`);
    }

    // --- A user prompt confirms too, and its confirmation is the delete one ---
    await openRow(page, USER_ID);
    let userConfirm = "";
    page.once("dialog", async (d) => {
      userConfirm = d.message();
      await d.dismiss();
    });
    mark = since();
    await clickReal(page, "#prompt-delete-btn");
    await page.waitForTimeout(300);
    if (!userConfirm) failures.push("deleting a user prompt did not ask for confirmation");
    if (userConfirm && !/cannot be undone/i.test(userConfirm)) {
      failures.push(`user-prompt confirm reads ${JSON.stringify(userConfirm)}, expected the delete wording`);
    }
    if (userConfirm && /vendor/i.test(userConfirm)) {
      failures.push(`user-prompt confirm used the RESTORE wording: ${JSON.stringify(userConfirm)}`);
    }
    if (sentSince(mark).some((r) => r.startsWith("DELETE"))) {
      failures.push("dismissing the user-prompt confirm still sent a DELETE");
    }

    // --- (6) Cancel must not leave a "Restore" button over an empty editor ---
    // Cancel clears the form and the id it acts on, so the label has to go with
    // it. Otherwise a red button still reading "Restore" sits above an empty
    // form and silently does nothing when clicked — it no-ops on the empty id.
    await openRow(page, RESTORED_ID);
    if ((await label(page)) !== "Restore") {
      failures.push("re-opening the restored system prompt did not read Restore");
    }
    // Cancel only appears once the form holds an id (setPromptFormMode).
    await page.waitForSelector("#prompt-cancel-btn:not(.hidden)", { state: "visible" });
    await clickReal(page, "#prompt-cancel-btn");
    await settled(page);
    const afterCancelHidden = await page.$eval("#prompt-delete-btn", (el) => el.classList.contains("hidden"));
    if (!afterCancelHidden) {
      failures.push(`after Cancel the button is still shown, reading ${JSON.stringify(await label(page))} over an empty editor`);
    }

    // --- (7) Duplicate must not keep the Restore label over an unsaved copy ---
    // Duplicate opens an unsaved NEW prompt. Carrying the system flag over would
    // put "Restore" above a copy that has never been saved.
    await openRow(page, RESTORED_ID);
    // duplicatePrompt() also awaits an api() call before filling the form, so
    // wait for the copy to actually land rather than for the layout to settle:
    // it clears the edit-id and puts the "(copy)" name in the field.
    await clickReal(page, "#prompt-duplicate-btn");
    await page.waitForFunction(
      () =>
        document.getElementById("prompt-edit-id")?.value === "" &&
        (document.getElementById("prompt-name-input")?.value || "").includes("copy")
    );
    await settled(page);
    const dupHidden = await page.$eval("#prompt-delete-btn", (el) => el.classList.contains("hidden"));
    if (!dupHidden) {
      const dupLabel = await label(page);
      if (dupLabel === "Restore") {
        failures.push("after Duplicate the button still reads Restore, over an unsaved copy");
      }
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}

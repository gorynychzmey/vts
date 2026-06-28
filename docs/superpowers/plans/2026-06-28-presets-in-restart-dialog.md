# Presets in the Restart Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a preset dropdown to the restart-final dialog that, on selection, fills the prompt multiselect with the preset's prompts (dangling user refs filtered); the dialog otherwise behaves as today and only `prompts` is sent.

**Architecture:** Pure frontend. The restart dialog already loads `/api/prompts`, pre-selects the task's current prompts into a flat multiselect, and POSTs `{mode:"final_only", prompts}`. We insert a `<select id="restart-final-preset">` above the multiselect, populate it from the existing `presetsCache`, and on change re-render the multiselect with `preset.options.prompts` filtered via the existing `filterDanglingPrompts`. No backend changes.

**Tech Stack:** Vanilla JS + HTML + CSS; verifier-web (Playwright) for UI checks.

**Spec:** [docs/superpowers/specs/2026-06-28-presets-in-restart-dialog-design.md](../specs/2026-06-28-presets-in-restart-dialog-design.md)

## Global Constraints

- Only `preset.options.prompts` is used from a preset in this dialog; `language`/`audio_only`/`transcript` are ignored (not applicable to a final restart).
- The dialog's default behavior is unchanged: dropdown opens on a neutral "—" item; the multiselect is pre-filled with the task's current prompts (`task.options.prompts` or `[{source:"system",id:"summary"}]`).
- Reuse existing module-level helpers: `presetsCache` (filled by `loadPresets()` at bootstrap), `presetLabel(preset)` (localizes system names by id), `filterDanglingPrompts(refs) -> {filtered, dangling}` (keeps system refs, drops user refs not in `promptsCache`), `renderPromptMultiselect(container, prompts, selectedRefs, {flat:true})`, `getSelectedFrom(container)`, `t(key)`.
- DOM-order: the new `<select>` markup goes inside `#restart-final-dialog`, which is already before the `<script>` tag — keep it there.
- No backend / endpoint change. `TaskCreateRequest` and the restart endpoint are untouched.
- Do NOT bump the version in this task (a later release bump/tag handles it).
- i18n keys added in all three locales `vts/static/i18n/{en,ru,de}.js`.

---

## File Structure

All changes are localized:
- `vts/static/index.html` — add the `<select id="restart-final-preset">` inside `#restart-final-dialog`, above `#restart-final-select`.
- `vts/static/app.js` — grab the new element; populate it on dialog open; reset to "—" on open; on change apply the preset's prompts.
- `vts/static/styles.css` — minimal styling for the preset row in the dialog (reuse existing classes where possible).
- `vts/static/i18n/{en,ru,de}.js` — two keys.
- `tests/ui/scenarios/restart-dialog.mjs` — extend to assert the preset dropdown + apply behavior.

---

## Task 1: Preset dropdown in the restart-final dialog

**Files:**
- Modify: `vts/static/index.html` (`#restart-final-dialog`, ~line 607-622)
- Modify: `vts/static/app.js` (restart-final element grabs ~3272, `openRestartFinalDialog` ~3283)
- Modify: `vts/static/styles.css`
- Modify: `vts/static/i18n/{en,ru,de}.js`
- Test: `tests/ui/scenarios/restart-dialog.mjs` (extend) + verifier-web run

**Interfaces:**
- Consumes (existing, module-level in app.js): `presetsCache`, `presetLabel(preset)`, `filterDanglingPrompts(refs)->{filtered,dangling}`, `renderPromptMultiselect`, `getSelectedFrom`, `updateRestartFinalSubmitState`, `loadPresets()`, `t`.
- Produces: a populated `#restart-final-preset` select; on change the multiselect re-renders with the preset's filtered prompts.

- [ ] **Step 1: Add the markup**

In `vts/static/index.html`, inside `<dialog id="restart-final-dialog">`, immediately BEFORE `<div class="prompt-select" id="restart-final-select"></div>` (currently line 619), insert:

```html
      <div class="restart-final-preset-row">
        <span class="preset-label" data-i18n="restart_final.preset">Preset</span>
        <select id="restart-final-preset"></select>
      </div>
```

- [ ] **Step 2: Grab the new element**

In `vts/static/app.js`, next to the existing restart-final grabs (after line 3275 `const restartFinalSubmitBtn = ...`), add:

```javascript
const restartFinalPreset = document.getElementById("restart-final-preset");
```

- [ ] **Step 3: Populate + reset the dropdown on dialog open**

In `openRestartFinalDialog(task)` (app.js ~3283), AFTER the existing
`renderPromptMultiselect(restartFinalSelect, prompts, selected, { flat: true });`
line and BEFORE `updateRestartFinalSubmitState();`, add a call to populate the
preset dropdown:

```javascript
  await populateRestartFinalPresets();
```

Add the helper (module-level, near `openRestartFinalDialog`):

```javascript
async function populateRestartFinalPresets() {
  if (!restartFinalPreset) {
    return;
  }
  if (!presetsCache.length) {
    await loadPresets();
  }
  restartFinalPreset.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = t("restart_final.preset_none");
  restartFinalPreset.appendChild(none);
  for (const preset of presetsCache) {
    const opt = document.createElement("option");
    opt.value = `${preset.source}:${preset.id}`;
    opt.textContent = presetLabel(preset);
    restartFinalPreset.appendChild(opt);
  }
  restartFinalPreset.value = ""; // reset to the neutral item on each open
}
```

> Note: `loadPresets()` (from vts-hp7) also touches the create-form preset
> dropdown. Calling it only when `presetsCache` is empty avoids redundant work;
> if it was already populated at bootstrap, this is a no-op fetch-wise.

- [ ] **Step 4: Apply the selected preset's prompts on change**

Add a change listener (near the other `restartFinal*` listeners, ~line 3308):

```javascript
restartFinalPreset?.addEventListener("change", () => {
  const value = restartFinalPreset.value;
  if (!value) {
    return; // "—" selected: leave the current multiselect as-is
  }
  const idx = value.indexOf(":");
  const source = value.slice(0, idx);
  const id = value.slice(idx + 1);
  const preset = presetsCache.find((p) => p.source === source && p.id === id);
  if (!preset) {
    return;
  }
  const promptRefs = (preset.options && preset.options.prompts) || [];
  const { filtered } = filterDanglingPrompts(promptRefs);
  renderPromptMultiselect(restartFinalSelect, promptsCache, filtered, { flat: true });
  updateRestartFinalSubmitState();
});
```

> `filterDanglingPrompts` reads the module-level `promptsCache`, which is
> populated by `loadPrompts()` at bootstrap and by the dialog's own
> `await api("/api/prompts")` path is into a LOCAL var — but `promptsCache` is
> already filled at bootstrap, so the filter has the full prompt list. The
> re-render uses `promptsCache` as the list of all prompts (same list the dialog
> renders from on open via its local `prompts`; both come from `/api/prompts`).

- [ ] **Step 5: CSS**

In `vts/static/styles.css`, add a small rule for the preset row in the dialog (reuse `.preset-label` which already exists):

```css
.restart-final-preset-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.6rem;
}

.restart-final-preset-row #restart-final-preset {
  min-width: 10rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  font-size: 0.84rem;
}
```

- [ ] **Step 6: i18n (all three locales)**

Add to `vts/static/i18n/en.js`, `ru.js`, `de.js` (next to other `restart_final.*` or `preset.*` keys):

- en: `"restart_final.preset": "Preset"`, `"restart_final.preset_none": "—"`
- ru: `"restart_final.preset": "Пресет"`, `"restart_final.preset_none": "—"`
- de: `"restart_final.preset": "Preset"`, `"restart_final.preset_none": "—"`

- [ ] **Step 7: Extend the verifier-web scenario**

In `tests/ui/scenarios/restart-dialog.mjs`, after the dialog is opened, add assertions for the preset dropdown. The scenario already overrides `/api/tasks` with a completed task and opens the dialog from the menu; extend the stub to also serve `/api/presets` and assert:

- `#restart-final-preset` exists and its current value is `""` (the "—" item) right after open.
- Selecting the user preset option (set `.value` to `user:<id>` and dispatch `change`) re-renders the multiselect so the checked prompts match the preset's prompts.

Concretely, in the scenario's `startStubServer({...})` call add a preset to the overrides:

```javascript
  "/api/presets": [
    { source: "system", id: "default", name: "Default", editable: false,
      options: { language: null, audio_only: false, transcript: true,
                 prompts: [{ source: "system", id: "summary" }] } },
    { source: "user", id: "p1", name: "Memo preset", editable: true,
      options: { language: null, audio_only: false, transcript: true,
                 prompts: [{ source: "user", id: "u1" }] } },
  ],
  "/api/me/default_preset": { source: "system", id: "default" },
```

(Ensure `/api/prompts` in the scenario includes the `user:u1` prompt so it is not filtered as dangling — e.g. add `{source:"user", id:"u1", name:"Memo", editable:true}` to the `/api/prompts` override if the scenario sets one; the harness default `/api/prompts` already includes `user:u1` "Memo".)

After opening the dialog, add:

```javascript
    // Preset dropdown present and neutral by default
    const presetVal = await page.evaluate(() => {
      const el = document.getElementById("restart-final-preset");
      return el ? el.value : "__missing__";
    });
    if (presetVal !== "") failures.push(`restart preset dropdown not neutral on open (got ${JSON.stringify(presetVal)})`);

    // Selecting the user preset applies its prompts to the multiselect
    const applied = await page.evaluate(() => {
      const el = document.getElementById("restart-final-preset");
      el.value = "user:p1";
      el.dispatchEvent(new Event("change", { bubbles: true }));
      const checked = [...document.querySelectorAll('#restart-final-select input[type="checkbox"]:checked')]
        .map((c) => `${c.dataset.source}:${c.dataset.id}`);
      return checked;
    });
    if (!(applied.length === 1 && applied[0] === "user:u1")) {
      failures.push(`preset apply did not set prompts to [user:u1] (got ${JSON.stringify(applied)})`);
    }
```

- [ ] **Step 8: Verify**

Run:
```bash
node --check /home/victor/dev/vts/vts/static/app.js
for f in en ru de; do node --check /home/victor/dev/vts/vts/static/i18n/$f.js; done
cd /home/victor/dev/vts/tests/ui && node run.mjs
```
Expected: `node --check` clean; `UI VERIFY: PASSED` with `restart-dialog` (now asserting the preset dropdown) among the passing scenarios.

- [ ] **Step 9: Commit**

```bash
git add vts/static/index.html vts/static/app.js vts/static/styles.css vts/static/i18n/ tests/ui/scenarios/restart-dialog.mjs
git commit -m "feat(ui): preset dropdown in restart dialog applies its prompts (vts-2or)"
```

---

## Self-Review Notes

**Spec coverage:**
- Dropdown above the multiselect; neutral "—" default; multiselect pre-filled with task's set (unchanged) → Steps 1,3. ✓
- Populate from presetsCache (loadPresets if empty); system names localized by id (presetLabel) → Step 3. ✓
- Select preset → take options.prompts, filter dangling, re-render multiselect, update submit state → Step 4. ✓
- Return to "—" is a no-op → Step 4 (early return on empty value). ✓
- Submit unchanged (only prompts) → no change needed (existing submit handler untouched). ✓
- i18n two keys in three locales → Step 6. ✓
- verifier-web extension → Step 7; closed-state already covered by the existing scenario. ✓
- No backend change; version not bumped → Global Constraints. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `restartFinalPreset`, `populateRestartFinalPresets()`, `presetLabel`, `filterDanglingPrompts(refs)->{filtered}`, `renderPromptMultiselect(container, promptsCache, filtered, {flat:true})`, `getSelectedFrom`, value format `"${source}:${id}"` split on first colon — consistent with the create-form preset dropdown (Task 8 of vts-hp7) and the existing restart submit handler.

# Player Transcript Autoscroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On `/player`, auto-scroll the transcript so the currently-playing sentence stays visible, gated by a default-ON checkbox that unchecks when the user scrolls manually.

**Architecture:** All markup, style, and logic live inline in `_player_page_html` (`vts/api/main.py`) — the `/player` page is a self-contained HTML document with no access to `app.js`. Autoscroll hooks into the existing `timeupdate` active-cue transition; a `programmaticScroll` flag plus a short debounce distinguishes our own smooth scroll from a user scroll.

**Tech Stack:** Python (FastAPI HTML rendering), vanilla inline JS, pytest.

## Global Constraints

- All page JS/CSS/markup is inline in `_player_page_html` — no `app.js`, no external assets.
- The inline `<script>` is inside a Python f-string: every literal `{` / `}` in JS must be doubled (`{{` / `}}`). Interpolations like `{live_script}` stay single-braced.
- Localized strings embed all locales (en/ru/de) and pick one client-side from `navigator.language` (same pattern as `_PLAYER_MEDIA_UNAVAILABLE_MSG`).
- Generated page `<script>` must pass `node --check` (both media-present and media-gone variants).
- Test DB is real Postgres; container `vts-test-pg` must be up on `127.0.0.1:5432` for endpoint tests. Pure-render tests need no DB.
- Run pytest via `.venv/bin/python -m pytest ... -p no:cacheprovider`.
- Checkbox default state is checked (ON). Not persisted across loads (out of scope).

---

### Task 1: Render the autoscroll checkbox in the player page

Add a localized, default-checked "Autoscroll" checkbox between the media element and the transcript, plus its CSS. No behaviour yet — this task only proves the control renders.

**Files:**
- Modify: `vts/api/main.py` — `_player_page_html` (template body ~line 1049-1050; inline `<style>` ~line 1043) and a new locale-map constant near `_PLAYER_MEDIA_UNAVAILABLE_MSG` (~line 812).
- Test: `tests/test_player_page.py`

**Interfaces:**
- Consumes: existing `_player_page_html(*, title, media_tag, blocks, task_id=None, as_user=None)`.
- Produces: rendered HTML contains `<input type="checkbox" id="autoscroll-toggle" checked>` and a label whose text is localized client-side; a `_PLAYER_AUTOSCROLL_MSG` dict `{ "en": "Autoscroll", "ru": "Автопрокрутка", "de": "Auto-Scrollen" }`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_player_page.py`:

```python
def test_player_html_has_autoscroll_checkbox_checked_by_default():
    blocks = [{"start": 0.0, "end": 1.0, "text": "hi", "label": "",
               "sentences": [{"start": 0.0, "end": 1.0, "text": "hi"}]}]
    html = _player_page_html(title="t", media_tag="<audio></audio>", blocks=blocks)
    # The checkbox exists and is checked by default.
    assert 'id="autoscroll-toggle"' in html
    assert "checked" in html
    # All three locale labels are embedded for client-side pick.
    assert "Autoscroll" in html
    assert "Автопрокрутка" in html
    assert "Auto-Scrollen" in html


def test_player_html_no_autoscroll_checkbox_when_media_gone():
    # Media gone -> no transcript, so no autoscroll control either.
    html = _player_page_html(title="t", media_tag=None, blocks=[])
    assert 'id="autoscroll-toggle"' not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_player_page.py::test_player_html_has_autoscroll_checkbox_checked_by_default tests/test_player_page.py::test_player_html_no_autoscroll_checkbox_when_media_gone -q -p no:cacheprovider`
Expected: FAIL — `id="autoscroll-toggle"` not present.

- [ ] **Step 3: Add the locale map constant**

Near `_PLAYER_MEDIA_UNAVAILABLE_MSG` (~line 812 in `vts/api/main.py`), add:

```python
_PLAYER_AUTOSCROLL_MSG = {
    "en": "Autoscroll",
    "ru": "Автопрокрутка",
    "de": "Auto-Scrollen",
}
```

- [ ] **Step 4: Render the checkbox row (only when there is a transcript)**

In `_player_page_html`, the checkbox belongs with the transcript — render it only when `transcript_html` is non-empty (media present + blocks). Build a small fragment after `transcript_html` is computed:

```python
    import json as _json_ac
    autoscroll_html = ""
    if transcript_html:
        ac_msgs = _json_ac.dumps(_PLAYER_AUTOSCROLL_MSG, ensure_ascii=False)
        autoscroll_html = (
            '<label class="autoscroll-toggle">'
            '<input type="checkbox" id="autoscroll-toggle" checked>'
            f"<span data-autoscroll-label data-msgs='{ac_msgs}'>"
            f"{_html.escape(_PLAYER_AUTOSCROLL_MSG['en'])}</span>"
            "</label>"
        )
```

Then place it between the media block and the transcript in the returned template — change:

```
{media_block}
{transcript_html}
```

to:

```
{media_block}
{autoscroll_html}
{transcript_html}
```

- [ ] **Step 5: Add checkbox CSS**

In the inline `<style>` (after the `.media-unavailable` rule, ~line 1044), add (note doubled braces):

```
  .autoscroll-toggle {{ display: flex; align-items: center; gap: 0.4rem;
    width: min(960px, 100%); margin: 0.8rem 0 0; color: #bbb;
    font-size: 0.85rem; cursor: pointer; }}
  .autoscroll-toggle input {{ cursor: pointer; }}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_player_page.py -q -p no:cacheprovider`
Expected: PASS (new tests + existing player tests still green).

- [ ] **Step 7: Verify generated JS still parses**

Run:
```bash
VTS_OAUTH_CLIENT_SECRET=x .venv/bin/python -c "
import vts.core.config as c; c._load_yaml_overrides=lambda:{}; c.get_settings.cache_clear()
from vts.api.main import _player_page_html
import re
h=_player_page_html(title='T', media_tag='<video src=\"/m\"></video>', blocks=[{'start':0.0,'end':1.0,'text':'hi','label':'','sentences':[{'start':0.0,'end':1.0,'text':'hi'}]}], task_id='x')
open('/tmp/ac1.js','w').write(re.search(r'<script>(.*)</script>', h, re.S).group(1))
"
node --check /tmp/ac1.js && echo OK
```
Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
git add vts/api/main.py tests/test_player_page.py
git commit -m "feat(player): render default-on autoscroll checkbox (vts-eho)"
```

---

### Task 2: Localize the checkbox label client-side

The label text must resolve to the viewer's language, mirroring the media-unavailable localizer.

**Files:**
- Modify: `vts/api/main.py` — inline `<script>` in `_player_page_html`, near the existing media-unavailable localizer (the `[data-media-unavailable]` block, ~line 1055).
- Test: `tests/test_player_page.py`

**Interfaces:**
- Consumes: the `<span data-autoscroll-label data-msgs='...'>` from Task 1.
- Produces: a client-side localizer that sets the span's text from `navigator.language`; no new server-side output beyond Task 1.

- [ ] **Step 1: Write the failing test**

```python
def test_player_html_localizes_autoscroll_label_client_side():
    blocks = [{"start": 0.0, "end": 1.0, "text": "hi", "label": "",
               "sentences": [{"start": 0.0, "end": 1.0, "text": "hi"}]}]
    html = _player_page_html(title="t", media_tag="<audio></audio>", blocks=blocks)
    # A dedicated client-side localizer resolves the label from navigator.language.
    assert "data-autoscroll-label" in html
    assert "labelEl" in html  # the localizer variable — absent until Step 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_player_page.py::test_player_html_localizes_autoscroll_label_client_side -q -p no:cacheprovider`
Expected: FAIL — `data-autoscroll-label` is present (from Task 1) but `labelEl` (the localizer) is not added until Step 3.

- [ ] **Step 3: Add the client-side label localizer**

In the inline `<script>`, right after the existing `[data-media-unavailable]` localizer block (~line 1065, before `wireCues` is defined), add (doubled braces):

```
  var labelEl = document.querySelector("[data-autoscroll-label]");
  if (labelEl) {{
    try {{
      var acMsgs = JSON.parse(labelEl.getAttribute("data-msgs") || "{{}}");
      var acLangs = (navigator.languages || [navigator.language || "en"]);
      for (var ai = 0; ai < acLangs.length; ai++) {{
        var acCode = String(acLangs[ai] || "").slice(0, 2).toLowerCase();
        if (acMsgs[acCode]) {{ labelEl.textContent = acMsgs[acCode]; break; }}
      }}
    }} catch (e) {{ /* keep the default English label */ }}
  }}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_player_page.py::test_player_html_localizes_autoscroll_label_client_side -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Verify generated JS parses**

Run the Task 1 Step 7 `node --check` snippet again.
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add vts/api/main.py tests/test_player_page.py
git commit -m "feat(player): localize autoscroll label client-side (vts-eho)"
```

---

### Task 3: Autoscroll on active-sentence change + user-scroll detection

Wire the actual behaviour: scroll the active cue to center when the box is checked; uncheck the box on a user scroll (via a `programmaticScroll` flag + debounce); re-checking re-centers the active cue.

**Files:**
- Modify: `vts/api/main.py` — inline `<script>`: the `timeupdate` handler (~line 1086-1098) and a new autoscroll wiring block after `wireCues(media)` (~line 1102, before `{live_script}`).
- Test: `tests/test_player_page.py`

**Interfaces:**
- Consumes: `#autoscroll-toggle` checkbox (Task 1), the `.transcript` container, `.cue.active` set by the `timeupdate` handler.
- Produces: no server API change — behaviour only. The rendered script contains `scrollIntoView`, `block: "center"`, a `programmaticScroll` guard, a debounce timer, and a checkbox `change` handler.

- [ ] **Step 1: Write the failing test**

```python
def test_player_html_autoscroll_logic_present():
    blocks = [{"start": 0.0, "end": 1.0, "text": "hi", "label": "",
               "sentences": [{"start": 0.0, "end": 1.0, "text": "hi"}]}]
    html = _player_page_html(
        title="t", media_tag='<video src="/m"></video>', blocks=blocks, task_id="x"
    )
    # Scrolls the active cue to center.
    assert "scrollIntoView" in html
    assert 'block: "center"' in html
    # Guards our own scroll and reacts to the checkbox.
    assert "programmaticScroll" in html
    assert "autoscroll-toggle" in html
    # A user scroll unchecks the box (checked = false somewhere in the handler).
    assert "checked = false" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_player_page.py::test_player_html_autoscroll_logic_present -q -p no:cacheprovider`
Expected: FAIL — `scrollIntoView` not present.

- [ ] **Step 3: Add a scroll-to-active helper and the active-cue hook**

In the inline `<script>`, extend the active-cue transition in the `timeupdate` handler. Change the transition block (currently):

```
      if (current !== active) {{
        if (active) active.classList.remove("active");
        if (current) current.classList.add("active");
        active = current;
      }}
```

to:

```
      if (current !== active) {{
        if (active) active.classList.remove("active");
        if (current) current.classList.add("active");
        active = current;
        if (current) maybeAutoscroll(current);
      }}
```

- [ ] **Step 4: Add the autoscroll wiring block**

After `wireCues(media);` and before `{live_script}` (~line 1102), add (doubled braces):

```
  // --- Autoscroll (vts-eho) ---
  var scrollBox = document.querySelector(".transcript");
  var autoToggle = document.getElementById("autoscroll-toggle");
  var programmaticScroll = false;
  var programmaticScrollTimer = null;

  function scrollCueToCenter(cue) {{
    if (!cue || !scrollBox) return;
    // Mark this scroll as ours so the scroll listener doesn't treat the
    // smooth-scroll's own events as a user gesture. Cleared on a debounce
    // after the animation's events settle.
    programmaticScroll = true;
    if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer);
    programmaticScrollTimer = setTimeout(function() {{
      programmaticScroll = false;
    }}, 150);
    cue.scrollIntoView({{ block: "center", behavior: "smooth" }});
  }}

  function maybeAutoscroll(cue) {{
    if (autoToggle && autoToggle.checked) scrollCueToCenter(cue);
  }}

  if (scrollBox) {{
    scrollBox.addEventListener("scroll", function() {{
      // Our own smooth-scroll fires scroll events too; ignore those.
      if (programmaticScroll) return;
      // A genuine user scroll turns autoscroll off.
      if (autoToggle && autoToggle.checked) autoToggle.checked = false;
    }});
  }}

  if (autoToggle) {{
    autoToggle.addEventListener("change", function() {{
      // Re-enabling brings the current sentence back into view.
      if (autoToggle.checked) {{
        var cur = document.querySelector(".cue.active");
        if (cur) scrollCueToCenter(cur);
      }}
    }});
  }}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_player_page.py::test_player_html_autoscroll_logic_present -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Verify generated JS parses (both variants)**

Run:
```bash
VTS_OAUTH_CLIENT_SECRET=x .venv/bin/python -c "
import vts.core.config as c; c._load_yaml_overrides=lambda:{}; c.get_settings.cache_clear()
from vts.api.main import _player_page_html
import re
for mt in ['<video src=\"/m\"></video>', None]:
    h=_player_page_html(title='T', media_tag=mt, blocks=([] if mt is None else [{'start':0.0,'end':1.0,'text':'hi','label':'','sentences':[{'start':0.0,'end':1.0,'text':'hi'}]}]), task_id='x')
    open('/tmp/ac.js','w').write(re.search(r'<script>(.*)</script>', h, re.S).group(1))
    import subprocess; subprocess.run(['node','--check','/tmp/ac.js'], check=True)
print('BOTH OK')
"
```
Expected: `BOTH OK`.

- [ ] **Step 7: Run the full player suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_player_page.py tests/test_player_transcript.py tests/test_transcript_updated_event.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add vts/api/main.py tests/test_player_page.py
git commit -m "feat(player): autoscroll active sentence, uncheck on user scroll (vts-eho)"
```

---

### Task 4: Version bump + final verification

**Files:**
- Modify: `vts/__init__.py`

**Interfaces:**
- Consumes: nothing. Produces: bumped `__version__`.

- [ ] **Step 1: Bump the version**

In `vts/__init__.py`, increment the patch: `__version__ = "1.5.24"` (or next patch above current — check with `grep '__version__' vts/__init__.py` first).

- [ ] **Step 2: Full backend suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: all pass (prior count + the 4 new autoscroll tests).

- [ ] **Step 3: Commit**

```bash
git add vts/__init__.py
git commit -m "chore: bump version for player autoscroll (vts-eho)"
```

---

## Post-plan notes

- `verifier-web` does not cover `/player` (backend route, not static) — no UI-verifier scenario is added. Final behavioural confirmation (scroll follows playback; user scroll unchecks; re-check re-centers) is a manual check in the real player after deploy.
- The checkbox lives outside `.transcript`, so `rebuildTranscript` (fired on `transcript_updated`) does not touch it; no extra handling needed. `maybeAutoscroll` re-queries `.cue.active` live, so it keeps working after a rebuild.

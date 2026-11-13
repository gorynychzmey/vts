(function() {
  // Entry point of ui://vts/summary: a recording's summary, with a switcher
  // to its other prompt results. Another result is fetched through
  // get_recording_prompt_result, the same tool a model would read it with.
  var MSGS = {
    en: { loading: "Loading…", failed: "The result could not be loaded.", empty: "This result is empty.",
      cancelled: "Cancelled.", untitled: "Untitled recording", summary: "Summary",
      regen: "Regenerate…", run: "Re-run", cancel: "Cancel",
      warning: "All prompt results of this recording will be replaced by new ones. This takes a few minutes.",
      running: "Re-running the prompts… new results appear here when they are ready.",
      regenFailed: "Re-running the prompts failed." },
    ru: { loading: "Загружаю…", failed: "Не удалось загрузить результат.", empty: "Этот результат пуст.",
      cancelled: "Отменено.", untitled: "Запись без названия", summary: "Саммари",
      regen: "Перегенерировать…", run: "Запустить", cancel: "Отмена",
      warning: "Все результаты промптов этой записи будут заменены новыми. Это займёт несколько минут.",
      running: "Промпты перезапущены… новые результаты появятся здесь, когда будут готовы.",
      regenFailed: "Перезапуск промптов не удался." },
    de: { loading: "Wird geladen…", failed: "Das Ergebnis konnte nicht geladen werden.", empty: "Dieses Ergebnis ist leer.",
      cancelled: "Abgebrochen.", untitled: "Aufnahme ohne Titel", summary: "Zusammenfassung",
      regen: "Neu erzeugen…", run: "Starten", cancel: "Abbrechen",
      warning: "Alle Prompt-Ergebnisse dieser Aufnahme werden durch neue ersetzt. Das dauert einige Minuten.",
      running: "Die Prompts laufen erneut… neue Ergebnisse erscheinen hier, sobald sie fertig sind.",
      regenFailed: "Der erneute Lauf der Prompts ist fehlgeschlagen." },
  };
  // For the model, not the user: always English.
  var SWITCHED = "In the VTS summary view the user is now reading";
  var RERUN = "In the VTS summary view the user re-ran the prompts";
  // Task statuses after which there is nothing more to wait for.
  var FINISHED = { completed: true, failed: true, canceled: true, archived: true, deleted: true };

  var bridge = VtsAppBridge.connect({ name: "vts-summary", version: "1" });
  var msgs = MSGS.en;
  var data = null;
  var showing = null;
  var request = 0; // which switch is current; a slow earlier answer is dropped

  function $(attr) { return document.querySelector("[" + attr + "]"); }
  function setStatus(text) { $("data-status").textContent = text || ""; }

  function applyHostContext() {
    var ctx = bridge.hostContext || {};
    var lang = String(ctx.locale || navigator.language || "en").slice(0, 2).toLowerCase();
    msgs = MSGS[lang] || MSGS.en;
    if (ctx.theme) document.documentElement.setAttribute("data-theme", ctx.theme);
    var vars = ctx.styles && ctx.styles.variables;
    if (vars) for (var k in vars) if (k.indexOf("--") === 0) document.documentElement.style.setProperty(k, vars[k]);
    $("data-regen").textContent = msgs.regen;
    $("data-regen-run").textContent = msgs.run;
    $("data-regen-cancel").textContent = msgs.cancel;
    $("data-regen-warning").textContent = msgs.warning;
  }

  function reportSize() {
    bridge.reportSize(document.documentElement.scrollWidth, document.documentElement.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  function label(result) {
    return result.ref === "system:summary" ? msgs.summary : result.name;
  }

  function renderSwitcher() {
    var box = $("data-switcher");
    box.innerHTML = "";
    var results = data.results || [];
    // One result needs no switcher.
    box.hidden = results.length < 2;
    results.forEach(function(result) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("data-ref", result.ref);
      b.setAttribute("aria-pressed", String(result.ref === showing));
      b.textContent = label(result);
      b.addEventListener("click", function() { switchTo(result); });
      box.appendChild(b);
    });
  }

  function showMarkdown(text) {
    VtsMarkdown.render($("data-markdown"), text);
    setStatus(text && text.trim() ? "" : msgs.empty);
  }

  async function switchTo(result) {
    if (result.ref === showing) return;
    var mine = ++request;
    showing = result.ref;
    renderSwitcher();
    setStatus(msgs.loading);
    try {
      var res = await bridge.callTool("get_recording_prompt_result", {
        recording_id: data.recording_id, ref: result.ref,
      });
      if (mine !== request) return;
      showMarkdown((res.structuredContent || {}).content || "");
      bridge.updateModelContext(SWITCHED + " \"" + result.name + "\" (" + result.ref
        + ") of recording " + data.recording_id + ".");
    } catch (e) {
      if (mine !== request) return;
      $("data-markdown").innerHTML = "";
      setStatus(msgs.failed);
    }
  }

  function show(next) {
    data = next || {};
    showing = data.ref;
    $("data-title").textContent = data.title || msgs.untitled;
    renderSwitcher();
    showMarkdown(data.markdown || "");
    var regen = data.regenerate || {};
    $("data-regen").hidden = !regen.available;
  }

  // --- Regenerate: every prompt re-run at once, as the web UI does ---
  // The previous results are cleared on the server before the new ones
  // exist, so the panel says so and runs nothing until the user confirms.
  function selectedRefs() {
    return Array.from(document.querySelectorAll("[data-regen-prompts] input:checked"))
      .map(function(box) { return box.value; });
  }

  function updateRunState() {
    $("data-regen-run").disabled = !selectedRefs().length;
  }

  async function openRegen() {
    var panel = $("data-regen-panel");
    var box = $("data-regen-prompts");
    box.innerHTML = "";
    panel.hidden = false;
    $("data-regen").hidden = true;
    var prompts = [];
    try {
      var res = await bridge.callTool("list_prompts", {});
      var sc = res.structuredContent || {};
      prompts = Array.isArray(sc) ? sc : (sc.result || []);
    } catch (e) {
      prompts = [];
    }
    var current = (data.regenerate && data.regenerate.current) || [];
    prompts.forEach(function(p) {
      var ref = p.source + ":" + p.id;
      var label = document.createElement("label");
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = ref;
      input.checked = current.indexOf(ref) >= 0;
      input.addEventListener("change", updateRunState);
      var text = document.createElement("span");
      text.textContent = ref === "system:summary" ? msgs.summary : p.name;
      label.appendChild(input);
      label.appendChild(text);
      box.appendChild(label);
    });
    updateRunState();
  }

  function closeRegen() {
    $("data-regen-panel").hidden = true;
    $("data-regen").hidden = !(data && data.regenerate && data.regenerate.available);
  }

  function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }

  async function runRegen() {
    var refs = selectedRefs();
    if (!refs.length) return;
    $("data-regen-run").disabled = true;
    var started;
    try {
      started = (await bridge.callTool("regenerate_summary", {
        recording_id: data.recording_id, prompts: refs,
      })).structuredContent || {};
    } catch (e) {
      $("data-regen-panel").hidden = true;
      setStatus(e.message || msgs.regenFailed);
      return;
    }
    $("data-regen-panel").hidden = true;
    $("data-switcher").hidden = true;
    $("data-markdown").innerHTML = "";
    setStatus(msgs.running);
    bridge.updateModelContext(RERUN + " " + refs.join(", ") + " for recording " + data.recording_id
      + " (task " + started.task_id + "); the new results are being produced.");
    var pollMs = Math.max(0.01, Number((data.regenerate || {}).poll_sec) || 5) * 1000;
    var status = "";
    // Bounded: a view left open on a task that never finishes stops asking
    // after an hour rather than polling forever.
    var deadline = Date.now() + 60 * 60 * 1000;
    while (Date.now() < deadline) {
      await sleep(pollMs);
      try {
        status = String(((await bridge.callTool("get_status", { task_id: started.task_id })).structuredContent || {}).status || "");
      } catch (e) {
        continue; // a missed poll is not a failure; ask again
      }
      if (FINISHED[status]) break;
    }
    if (status !== "completed") { setStatus(msgs.regenFailed); return; }
    try {
      var res = await bridge.callTool("show_summary", { recording_id: data.recording_id });
      show(res.structuredContent || {});
    } catch (e) {
      setStatus(msgs.failed);
    }
  }

  $("data-regen").addEventListener("click", openRegen);
  $("data-regen-cancel").addEventListener("click", closeRegen);
  $("data-regen-run").addEventListener("click", runRegen);

  bridge.on("tool-result", function(result) {
    if (result.isError) {
      setStatus(((result.content || [])[0] || {}).text || msgs.failed);
      return;
    }
    show(result.structuredContent || {});
  });
  bridge.on("tool-cancelled", function() { setStatus(msgs.cancelled); });
  bridge.on("host-context", applyHostContext);

  bridge.ready.then(function() {
    applyHostContext();
    setStatus(msgs.loading);
  }).catch(function() { setStatus(msgs.failed); });
})();

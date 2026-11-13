(function() {
  // Entry point of ui://vts/summary: a recording's summary, with a switcher
  // to its other prompt results. Another result is fetched through
  // get_recording_prompt_result, the same tool a model would read it with.
  var MSGS = {
    en: { loading: "Loading…", failed: "The result could not be loaded.", empty: "This result is empty.",
      cancelled: "Cancelled.", untitled: "Untitled recording", summary: "Summary" },
    ru: { loading: "Загружаю…", failed: "Не удалось загрузить результат.", empty: "Этот результат пуст.",
      cancelled: "Отменено.", untitled: "Запись без названия", summary: "Саммари" },
    de: { loading: "Wird geladen…", failed: "Das Ergebnis konnte nicht geladen werden.", empty: "Dieses Ergebnis ist leer.",
      cancelled: "Abgebrochen.", untitled: "Aufnahme ohne Titel", summary: "Zusammenfassung" },
  };
  // For the model, not the user: always English.
  var SWITCHED = "In the VTS summary view the user is now reading";

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

  bridge.on("tool-result", function(result) {
    if (result.isError) {
      setStatus(((result.content || [])[0] || {}).text || msgs.failed);
      return;
    }
    data = result.structuredContent || {};
    showing = data.ref;
    $("data-title").textContent = data.title || msgs.untitled;
    renderSwitcher();
    showMarkdown(data.markdown || "");
  });
  bridge.on("tool-cancelled", function() { setStatus(msgs.cancelled); });
  bridge.on("host-context", applyHostContext);

  bridge.ready.then(function() {
    applyHostContext();
    setStatus(msgs.loading);
  }).catch(function() { setStatus(msgs.failed); });
})();

(function() {
  // Entry point of ui://vts/search, the view on a search_transcripts result:
  // the passages found, each opening the player on its moment — inside this
  // view, through the same VtsPlayerView as ui://vts/player, without a round
  // trip through the model.
  var MSGS = {
    en: {
      found: function(n) { return n === 1 ? "1 passage" : n + " passages"; },
      none: "Nothing in your recordings matches closely enough.",
      more: "Show more",
      back: "← Back to results",
      failed: "The search could not be loaded.",
      opening: "Opening the recording…",
      untitled: "Untitled recording",
    },
    ru: {
      found: function(n) { return "Найдено фрагментов: " + n; },
      none: "В ваших записях нет ничего достаточно близкого.",
      more: "Показать ещё",
      back: "← К результатам",
      failed: "Не удалось загрузить поиск.",
      opening: "Открываю запись…",
      untitled: "Запись без названия",
    },
    de: {
      found: function(n) { return n === 1 ? "1 Passage" : n + " Passagen"; },
      none: "In Ihren Aufnahmen passt nichts nah genug.",
      more: "Mehr anzeigen",
      back: "← Zurück zu den Ergebnissen",
      failed: "Die Suche konnte nicht geladen werden.",
      opening: "Aufnahme wird geöffnet…",
      untitled: "Aufnahme ohne Titel",
    },
  };

  var bridge = VtsAppBridge.connect({ name: "vts-search", version: "1" });
  var msgs = MSGS.en;
  var args = {};
  var shown = 0;
  var total = 0;
  var query = "";
  var mounted = null;
  var opening = 0; // which open() is current; a stale answer is dropped

  function $(attr) { return document.querySelector("[" + attr + "]"); }
  var searchRoot = $("data-search-root");
  var playerRoot = $("data-player-root");

  function setStatus(text) { $("data-search-status").textContent = text || ""; }

  function applyHostContext() {
    var lang = VtsPlayerView.applyHostContext(bridge.hostContext);
    msgs = MSGS[lang] || MSGS.en;
    $("data-more").textContent = msgs.more;
    $("data-back").textContent = msgs.back;
  }

  function reportSize() {
    bridge.reportSize(document.documentElement.scrollWidth, document.documentElement.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  function hitItem(hit) {
    var li = document.createElement("li");
    li.className = "hit";
    li.setAttribute("role", "button");
    li.setAttribute("tabindex", "0");
    li.setAttribute("data-hit", "");
    var head = document.createElement("div");
    head.className = "hit-head";
    var title = document.createElement("span");
    title.className = "hit-title";
    title.textContent = hit.title || msgs.untitled;
    var when = document.createElement("span");
    when.className = "hit-when";
    when.textContent = VtsPlayerCore.timecode(hit.start_sec);
    head.appendChild(title);
    head.appendChild(when);
    if (hit.speakers && hit.speakers.length) {
      var who = document.createElement("span");
      who.className = "hit-who";
      who.textContent = hit.speakers.join(", ");
      head.appendChild(who);
    }
    var text = document.createElement("p");
    text.className = "hit-text";
    // Transcript text is data: set as text, never parsed as HTML.
    text.textContent = hit.text || "";
    li.appendChild(head);
    li.appendChild(text);
    var go = function() { open(hit); };
    li.addEventListener("click", go);
    li.addEventListener("keydown", function(e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
    return li;
  }

  function showResult(result) {
    var hits = result.hits || [];
    total = Number(result.total) || 0;
    shown += hits.length;
    var list = $("data-hits");
    hits.forEach(function(hit) { list.appendChild(hitItem(hit)); });
    $("data-search-title").textContent = query ? "«" + query + "»" : "";
    $("data-search-meta").textContent = total ? msgs.found(total) : "";
    if (!shown) setStatus(msgs.none);
    else setStatus("");
    // `total` is how many clear the threshold in the whole corpus, so it says
    // plainly whether there is more — a full-looking page does not.
    $("data-more").hidden = shown >= total;
  }

  async function more() {
    var button = $("data-more");
    button.disabled = true;
    try {
      var next = Object.assign({}, args, { offset: shown });
      var res = await bridge.callTool("search_transcripts", next);
      showResult(res.structuredContent || {});
    } catch (e) {
      setStatus(msgs.failed);
    }
    button.disabled = false;
  }

  async function open(hit) {
    var mine = ++opening;
    searchRoot.hidden = true;
    playerRoot.hidden = false;
    VtsPlayerView.setStatusIn(playerRoot, msgs.opening);
    playerRoot.querySelector("[data-title]").textContent = hit.title || "";
    playerRoot.querySelector("[data-meta]").textContent = "";
    playerRoot.querySelector("[data-media-host]").innerHTML = "";
    playerRoot.querySelector("[data-transcript-host]").innerHTML = "";
    try {
      var res = await bridge.callTool("show_recording", {
        recording_id: hit.recording_id, at_sec: hit.start_sec,
      });
      // Went back, or opened another hit, while this one loaded.
      if (mine !== opening) return;
      mounted = VtsPlayerView.mount(bridge, playerRoot, res.structuredContent || {});
    } catch (e) {
      if (mine !== opening) return;
      VtsPlayerView.setStatusIn(playerRoot, e.message || VtsPlayerView.msgs().failed);
    }
  }

  function back() {
    opening++;
    if (mounted) { mounted.unmount(); mounted = null; }
    playerRoot.hidden = true;
    searchRoot.hidden = false;
  }

  $("data-more").addEventListener("click", more);
  $("data-back").addEventListener("click", back);

  bridge.on("tool-input", function(params) {
    args = Object.assign({}, params.arguments || {});
    delete args.offset;
    query = String(args.query || "");
  });
  bridge.on("tool-result", function(result) {
    if (result.isError) {
      setStatus(((result.content || [])[0] || {}).text || msgs.failed);
      return;
    }
    var data = result.structuredContent || {};
    if (!query) query = String(data.query || "");
    showResult(data);
  });
  bridge.on("tool-cancelled", function() { setStatus(""); });
  bridge.on("host-context", applyHostContext);

  bridge.ready.then(function() {
    applyHostContext();
  }).catch(function() { setStatus(msgs.failed); });
})();

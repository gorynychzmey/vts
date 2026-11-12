(function() {
  // The MCP player view's adapter around VtsPlayerCore: data arrives through
  // the bridge (the show_recording result, then app_player_blocks pages)
  // instead of fetch and SSE as on the /player page.
  var MSGS = {
    en: {
      loading: "Loading the transcript…",
      empty: "This recording has no transcript yet.",
      failed: "The transcript could not be loaded.",
      cancelled: "Cancelled.",
      people: "People",
    },
    ru: {
      loading: "Загружаю транскрипт…",
      empty: "У этой записи пока нет транскрипта.",
      failed: "Не удалось загрузить транскрипт.",
      cancelled: "Отменено.",
      people: "Участники",
    },
    de: {
      loading: "Transkript wird geladen…",
      empty: "Diese Aufnahme hat noch kein Transkript.",
      failed: "Das Transkript konnte nicht geladen werden.",
      cancelled: "Abgebrochen.",
      people: "Personen",
    },
  };

  // For the model, not the user: always English, like the tool descriptions.
  var SELECTED = "In the VTS player the user selected the passage at";

  var bridge = VtsAppBridge.connect({ name: "vts-player", version: "1" });
  var msgs = MSGS.en;
  var player = null;

  function $(attr) { return document.querySelector("[" + attr + "]"); }

  function setStatus(text) { $("data-status").textContent = text || ""; }

  function applyHostContext(ctx) {
    var lang = String((ctx && ctx.locale) || navigator.language || "en").slice(0, 2).toLowerCase();
    msgs = MSGS[lang] || MSGS.en;
    if (ctx && ctx.theme) document.documentElement.setAttribute("data-theme", ctx.theme);
    var vars = ctx && ctx.styles && ctx.styles.variables;
    if (vars) {
      for (var name in vars) {
        if (name.indexOf("--") === 0) document.documentElement.style.setProperty(name, vars[name]);
      }
    }
  }

  function duration(seconds) {
    return seconds ? VtsPlayerCore.timecode(seconds) : "";
  }

  function renderHead(data) {
    $("data-title").textContent = data.title || "";
    var parts = [];
    if (data.duration_sec) parts.push(duration(data.duration_sec));
    if (data.people && data.people.length) parts.push(msgs.people + ": " + data.people.join(", "));
    $("data-meta").textContent = parts.join(" · ");
  }

  // The host sizes the iframe from what the view reports.
  function reportSize() {
    bridge.reportSize(document.documentElement.scrollWidth, document.documentElement.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  async function show(data) {
    renderHead(data);
    player = VtsPlayerCore.create({
      container: $("data-transcript-host"),
      media: null,
      autoscrollToggle: null,
      onSelect: function(sentence) {
        bridge.updateModelContext(
          SELECTED + " " + VtsPlayerCore.timecode(sentence.start)
          + " (" + sentence.start + " s) of recording " + data.recording_id + ": " + sentence.text
        );
      },
    });
    var blocks = (data.blocks || []).slice();
    if (!blocks.length && !data.next_cursor) { setStatus(msgs.empty); return; }
    player.render(blocks);
    // Later pages come through the app-only tool; the transcript is shown as
    // it grows, and a cited moment is positioned once its page has arrived.
    var cursor = data.next_cursor;
    var cited = typeof data.at_sec === "number";
    var positioned = false;
    function maybePosition(final) {
      if (!cited || positioned) return;
      if (final || player.cueAt(data.at_sec) && cursorBeyond(data.at_sec, blocks)) {
        player.showCited({ seconds: data.at_sec });
        positioned = true;
      }
    }
    maybePosition(!cursor);
    while (cursor) {
      setStatus(msgs.loading);
      try {
        var page = (await bridge.callTool("app_player_blocks", {
          recording_id: data.recording_id, cursor: cursor,
        })).structuredContent || {};
        blocks = blocks.concat(page.blocks || []);
        cursor = page.next_cursor || null;
        player.append(page.blocks || []);
        maybePosition(!cursor);
      } catch (e) {
        setStatus(msgs.failed);
        return;
      }
    }
    setStatus("");
  }

  // True once the loaded blocks reach past `seconds`, so the cue found for it
  // is the right one and not merely the last one loaded so far.
  function cursorBeyond(seconds, blocks) {
    var last = blocks[blocks.length - 1];
    var sentences = (last && last.sentences) || [];
    var end = sentences.length ? Number(sentences[sentences.length - 1].start) : -1;
    return end > seconds;
  }

  bridge.on("tool-result", function(result) {
    if (result.isError) {
      setStatus(((result.content || [])[0] || {}).text || msgs.failed);
      return;
    }
    show(result.structuredContent || {});
  });
  bridge.on("tool-cancelled", function() { setStatus(msgs.cancelled); });
  bridge.on("host-context", function() { applyHostContext(bridge.hostContext); });

  bridge.ready.then(function() {
    applyHostContext(bridge.hostContext);
    setStatus(msgs.loading);
  }).catch(function() { setStatus(msgs.failed); });
})();

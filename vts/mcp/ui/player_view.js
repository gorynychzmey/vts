// The MCP player, as a module: VtsPlayerView.mount(bridge, root, data) shows a
// recording inside `root`, an element holding the player skeleton
// ([data-title], [data-meta], [data-media-host], [data-status],
// [data-transcript-host]). Data arrives through the bridge — the
// show_recording result, then app_player_blocks pages and an app_media_url
// link — instead of fetch and SSE as on the /player page.
//
// Mounted by player_main.js for ui://vts/player, and by the search view when
// a hit is opened, so both show a recording the same way.
var VtsPlayerView = (function() {
  var MSGS = {
    en: {
      loading: "Loading the transcript…",
      empty: "This recording has no transcript yet.",
      failed: "The transcript could not be loaded.",
      cancelled: "Cancelled.",
      people: "People",
      autoscroll: "Autoscroll",
      mediaGone: "The media cannot be played here; the transcript is still available.",
    },
    ru: {
      loading: "Загружаю транскрипт…",
      empty: "У этой записи пока нет транскрипта.",
      failed: "Не удалось загрузить транскрипт.",
      cancelled: "Отменено.",
      people: "Участники",
      autoscroll: "Автопрокрутка",
      mediaGone: "Медиа здесь не воспроизводится; транскрипт доступен.",
    },
    de: {
      loading: "Transkript wird geladen…",
      empty: "Diese Aufnahme hat noch kein Transkript.",
      failed: "Das Transkript konnte nicht geladen werden.",
      cancelled: "Abgebrochen.",
      people: "Personen",
      autoscroll: "Auto-Scrollen",
      mediaGone: "Das Medium kann hier nicht abgespielt werden; das Transkript ist verfügbar.",
    },
  };

  // For the model, not the user: always English, like the tool descriptions.
  var SELECTED = "In the VTS player the user selected the passage at";

  var msgs = MSGS.en;

  // Theme, colours and language from the host. Returns the messages for the
  // host's language, for callers with strings of their own.
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
    return lang;
  }

  function duration(seconds) {
    return seconds ? VtsPlayerCore.timecode(seconds) : "";
  }

  // Each mount gets a generation number: a mount that is superseded (the
  // search view went back and opened another hit) stops fetching pages, so an
  // old recording cannot spill into the new one.
  var generation = 0;

  function mount(bridge, root, data) {
    var gen = ++generation;
    var live = function() { return gen === generation; };
    function $(attr) { return root.querySelector("[" + attr + "]"); }
    function setStatus(text) { if (live()) $("data-status").textContent = text || ""; }
    $("data-media-host").innerHTML = "";
    $("data-transcript-host").innerHTML = "";
    setStatus("");

    function renderHead(data) {
      $("data-title").textContent = data.title || "";
      var parts = [];
      if (data.duration_sec) parts.push(duration(data.duration_sec));
      if (data.people && data.people.length) parts.push(msgs.people + ": " + data.people.join(", "));
      $("data-meta").textContent = parts.join(" · ");
    }

    // The media, through a short-lived signed link from app_media_url. No link
    // (no media, no public address, a failed call) means a transcript without a
    // player — never an error. A link that stops working (it expired while the
    // view stayed open) is renewed, keeping the position — every time it
    // expires, since a view can stay open for hours. Only a renewed link that
    // fails before it loaded gives up: then the player goes, the transcript
    // stays, and there is no renew loop.
    async function mediaLink(recordingId) {
      try {
        var res = await bridge.callTool("app_media_url", { recording_id: recordingId });
        var link = res.structuredContent || {};
        return link.url ? link : null;
      } catch (e) {
        return null;
      }
    }

    async function loadMedia(recordingId) {
      var link = await mediaLink(recordingId);
      if (!link || !live()) return null;
      var host = $("data-media-host");
      var el = document.createElement(link.kind === "video" ? "video" : "audio");
      el.controls = true;
      el.preload = "metadata";
      el.src = link.url;
      host.appendChild(el);
      var label = document.createElement("label");
      label.className = "autoscroll-toggle";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.setAttribute("data-autoscroll", "");
      var text = document.createElement("span");
      text.textContent = msgs.autoscroll;
      label.appendChild(box);
      label.appendChild(text);
      host.appendChild(label);

      var renewing = false;
      el.addEventListener("loadedmetadata", function() { renewing = false; });
      el.addEventListener("error", async function() {
        if (!renewing) {
          renewing = true;
          var at = el.currentTime || 0;
          var fresh = await mediaLink(recordingId);
          if (!live()) return;
          if (fresh) {
            el.addEventListener("loadedmetadata", function() {
              try { el.currentTime = at; } catch (e) {}
            }, { once: true });
            el.src = fresh.url;
            return;
          }
        }
        host.innerHTML = "";
        var note = document.createElement("p");
        note.className = "status";
        note.setAttribute("data-media-gone", "");
        note.textContent = msgs.mediaGone;
        host.appendChild(note);
      });
      return { media: el, toggle: box };
    }

    async function show(data) {
      renderHead(data);
      var media = await loadMedia(data.recording_id);
      if (!live()) return;
      var player = VtsPlayerCore.create({
        container: $("data-transcript-host"),
        media: media ? media.media : null,
        autoscrollToggle: media ? media.toggle : null,
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
          if (!live()) return;
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

    show(data);
    return {
      // Stop: pause the media and fetch nothing more for this recording.
      unmount: function() {
        if (gen === generation) generation++;
        var m = $("data-media-host").querySelector("video, audio");
        if (m) m.pause();
      },
    };
  }

  // True once the loaded blocks reach past `seconds`, so the cue found for it
  // is the right one and not merely the last one loaded so far.
  function cursorBeyond(seconds, blocks) {
    var last = blocks[blocks.length - 1];
    var sentences = (last && last.sentences) || [];
    var end = sentences.length ? Number(sentences[sentences.length - 1].start) : -1;
    return end > seconds;
  }

  return {
    mount: mount,
    applyHostContext: applyHostContext,
    msgs: function() { return msgs; },
    setStatusIn: function(root, text) {
      var el = root.querySelector("[data-status]");
      if (el) el.textContent = text || "";
    },
  };
})();

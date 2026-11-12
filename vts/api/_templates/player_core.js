// Player core shared by the /player page and the MCP Apps player view
// (vts-vtma.1). It renders the two-level transcript and wires it to a media
// element: click and keyboard seek, the playing-line highlight, autoscroll and
// its pause on a manual scroll, and positioning on a cited moment.
//
// It knows nothing about where its data comes from — no fetch, no SSE, no URL,
// no i18n. Each page brings those through a thin adapter and hands the core
// blocks in the shape `_load_player_blocks()` returns:
//   [{label?: string, sentences: [{start: number, text: string}, ...]}, ...]
//
// It is the ONLY renderer of the transcript. There used to be two (Python on
// first paint, JS on a live rebuild), and they disagreed on which sentences to
// skip, so a rebuild could renumber `data-cue` under a deep link.
var VtsPlayerCore = (function() {
  function timecode(start) {
    var s = Math.max(0, Math.floor(Number(start) || 0));
    var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh
      ? hh + ":" + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0")
      : mm + ":" + String(ss).padStart(2, "0");
  }

  // A sentence is shown only with a usable start and some text: a cue that
  // cannot seek, or has nothing to click on, must not take a number either.
  function usableSentences(block) {
    var out = [];
    var list = (block && block.sentences) || [];
    for (var i = 0; i < list.length; i++) {
      var start = parseFloat(list[i] && list[i].start);
      var text = String((list[i] && list[i].text) || "").trim();
      if (!isNaN(start) && text) out.push({ start: start, text: text });
    }
    return out;
  }

  // opts:
  //   container        element the transcript list is appended to
  //   media            the video or audio element, or null for a transcript
  //                    without media
  //   autoscrollToggle checkbox driving autoscroll, or null (always follows)
  //   onSelect         optional; called with {start, text, cue} when the user
  //                    picks a sentence (click or keyboard)
  function create(opts) {
    var container = opts.container;
    var media = opts.media || null;
    var autoToggle = opts.autoscrollToggle || null;
    var onSelect = typeof opts.onSelect === "function" ? opts.onSelect : null;
    var list = null;
    var active = null;
    var programmaticScroll = false;
    var programmaticScrollTimer = null;
    // The cited moment survives a re-render: a transcript that arrives or
    // grows after the link was followed (a live rebuild on /player, the next
    // page in the MCP view) must still show the passage the link named.
    var cited = null;        // {cue, seconds} as last asked for
    var citedPlaced = false; // its sentence has been found and scrolled to

    function cues() {
      return list ? list.querySelectorAll(".cue") : [];
    }

    function scrollCueToCenter(cue) {
      if (!cue || !list) return;
      // Mark this scroll as ours so the scroll listener doesn't treat the
      // smooth-scroll's own events as a user gesture. Cleared on a debounce
      // after the animation's events settle.
      programmaticScroll = true;
      if (programmaticScrollTimer) clearTimeout(programmaticScrollTimer);
      programmaticScrollTimer = setTimeout(function() {
        programmaticScroll = false;
      }, 150);
      cue.scrollIntoView({ block: "center", behavior: "smooth" });
    }

    function autoscrollOn() {
      return !autoToggle || autoToggle.checked;
    }

    function seek(start, play) {
      if (!media || isNaN(start)) return;
      media.currentTime = start;
      if (play) {
        var p = media.play();
        // A refused play() (autoplay policy, a pause that overtook it) is not
        // an error worth surfacing: the seek happened, the user can press play.
        if (p && p.catch) p.catch(function() {});
      }
    }

    function buildCue(sentence, index) {
      var span = document.createElement("span");
      span.className = "cue";
      span.setAttribute("data-start", String(sentence.start));
      span.setAttribute("data-cue", String(index));
      span.setAttribute("role", "button");
      span.setAttribute("tabindex", "0");
      span.title = timecode(sentence.start);
      span.textContent = sentence.text;
      var pick = function() {
        seek(sentence.start, true);
        if (onSelect) onSelect({ start: sentence.start, text: sentence.text, cue: index });
      };
      span.addEventListener("click", pick);
      span.addEventListener("keydown", function(e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
      });
      return span;
    }

    function ensureList() {
      if (list) return list;
      list = document.createElement("ol");
      list.className = "transcript";
      list.addEventListener("scroll", function() {
        // Our own smooth-scroll fires scroll events too; ignore those.
        if (programmaticScroll) return;
        // A genuine user scroll turns autoscroll off.
        if (autoToggle && autoToggle.checked) autoToggle.checked = false;
      });
      container.appendChild(list);
      return list;
    }

    var nextIndex = 0;

    function appendBlocks(ol, blocks) {
      blocks.forEach(function(block) {
        var sentences = usableSentences(block);
        if (!sentences.length) return;
        var li = document.createElement("li");
        li.className = "block";
        var label = String((block && block.label) || "").trim();
        if (label) {
          var lab = document.createElement("div");
          lab.className = "block-label";
          lab.textContent = label;
          li.appendChild(lab);
        }
        var body = document.createElement("p");
        body.className = "block-body";
        sentences.forEach(function(sentence, i) {
          if (i) body.appendChild(document.createTextNode(" "));
          body.appendChild(buildCue(sentence, nextIndex++));
        });
        li.appendChild(body);
        ol.appendChild(li);
      });
      // No highlight here: the next timeupdate sets it. Highlighting on render
      // would also autoscroll, jumping the page on first load.
      if (cited) placeCited(!citedPlaced);
    }

    // Replace the transcript. Cues are numbered across the WHOLE transcript,
    // from 0 on every render: `data-cue` is what a deep link addresses, so it
    // names a sentence in the recording, not in its block or in a render.
    // An empty `blocks` leaves the current transcript in place.
    function render(blocks) {
      if (!Array.isArray(blocks) || !blocks.length) return;
      var ol = ensureList();
      ol.innerHTML = "";
      active = null;
      nextIndex = 0;
      appendBlocks(ol, blocks);
    }

    // Add the next part of a transcript that arrives in pages, numbering on
    // from where the last part stopped. What is already shown is left alone,
    // so the reader's scroll position and marks survive.
    function append(blocks) {
      if (!Array.isArray(blocks) || !blocks.length) return;
      appendBlocks(ensureList(), blocks);
    }

    // The last cue starting at or before `seconds`: the sentence a moment in
    // the recording belongs to.
    function cueAt(seconds) {
      var all = cues(), found = null;
      for (var i = 0; i < all.length; i++) {
        var start = parseFloat(all[i].getAttribute("data-start"));
        if (!isNaN(start) && start <= seconds + 0.001) found = all[i];
        else break;
      }
      return found;
    }

    function highlight() {
      if (!media) return;
      var current = cueAt(media.currentTime);
      if (current === active) return;
      if (active) active.classList.remove("active");
      if (current) current.classList.add("active");
      active = current;
      if (current && autoscrollOn()) scrollCueToCenter(current);
    }

    if (media) media.addEventListener("timeupdate", highlight);

    if (autoToggle) {
      autoToggle.addEventListener("change", function() {
        // Re-enabling brings the current sentence back into view.
        if (autoToggle.checked && active) scrollCueToCenter(active);
      });
    }

    // "cited" is deliberately NOT "active": they mean different things and can
    // sit on different sentences at once — one is what a link pointed at, the
    // other is what is playing now.
    function markCited(cue) {
      var previous = list && list.querySelector(".cue.cited");
      if (previous) previous.classList.remove("cited");
      if (cue) cue.classList.add("cited");
    }

    // The sentence a cited target names, and the moment to seek to.
    function resolveCited(target) {
      var seconds = typeof target.seconds === "number" && !isNaN(target.seconds)
        ? Math.max(0, target.seconds) : null;
      var cue = null;
      if (target.cue !== null && target.cue !== undefined && target.cue !== "" && list) {
        cue = list.querySelector('.cue[data-cue="' + String(target.cue).replace(/["\\]/g, "") + '"]');
        if (cue && seconds === null) {
          var cueStart = parseFloat(cue.getAttribute("data-start"));
          if (!isNaN(cueStart)) seconds = cueStart;
        }
      }
      if (cue === null && seconds !== null) cue = cueAt(seconds);
      return { cue: cue, seconds: seconds };
    }

    // Mark the cited sentence; on first placement also scroll to it. Later
    // re-renders only restore the mark, so a growing transcript does not keep
    // yanking the reader back.
    function placeCited(scroll) {
      var found = resolveCited(cited);
      markCited(found.cue);
      if (found.cue) {
        if (scroll) scrollCueToCenter(found.cue);
        citedPlaced = true;
      }
    }

    // Arrive at a cited moment: mark the sentence, scroll it into view, and
    // seek the media there WITHOUT playing — arriving at someone's recording
    // should not start sound the reader did not ask for.
    //   target.cue      sentence index (wins: a time can drift on re-render)
    //   target.seconds  a moment in the recording
    // Remembered, so a transcript rendered later still lands on it.
    function showCited(target) {
      if (!target) return;
      var found = resolveCited(target);
      if (found.cue === null && found.seconds === null
          && (target.cue === null || target.cue === undefined || target.cue === "")) return;
      cited = { cue: target.cue, seconds: target.seconds };
      citedPlaced = false;
      placeCited(true);
      var seconds = found.seconds;
      if (seconds === null && typeof target.seconds === "number" && !isNaN(target.seconds)) {
        seconds = Math.max(0, target.seconds);
      }
      if (media && seconds !== null) {
        var go = function() { try { media.currentTime = seconds; } catch (e) {} };
        if (media.readyState > 0) go();
        else media.addEventListener("loadedmetadata", go, { once: true });
      }
    }

    return { render: render, append: append, showCited: showCited, cueAt: cueAt };
  }

  return { create: create, timecode: timecode };
})();

(function() {
  // Localize the media-unavailable message client-side (the page has no
  // access to app.js i18n). Runs whether or not media is present.
  var mu = document.querySelector("[data-media-unavailable]");
  if (mu) {
    try {
      var msgs = JSON.parse(mu.getAttribute("data-msgs") || "{}");
      var langs = (navigator.languages || [navigator.language || "en"]);
      for (var li = 0; li < langs.length; li++) {
        var code = String(langs[li] || "").slice(0, 2).toLowerCase();
        if (msgs[code]) { mu.textContent = msgs[code]; break; }
      }
    } catch (e) { /* keep the default English text */ }
  }
  // Localize the autoscroll checkbox label client-side.
  var labelEl = document.querySelector("[data-autoscroll-label]");
  if (labelEl) {
    try {
      var acMsgs = JSON.parse(labelEl.getAttribute("data-msgs") || "{}");
      var acLangs = (navigator.languages || [navigator.language || "en"]);
      for (var ai = 0; ai < acLangs.length; ai++) {
        var acCode = String(acLangs[ai] || "").slice(0, 2).toLowerCase();
        if (acMsgs[acCode]) { labelEl.textContent = acMsgs[acCode]; break; }
      }
    } catch (e) { /* keep the default English label */ }
  }
  // Wire seek-on-click + active-cue highlight. Re-queries .cue each call so it
  // works after the transcript list is rebuilt from a transcript_updated event.
  function wireCues(media) {
    if (!media) return;
    var cues = Array.prototype.slice.call(document.querySelectorAll(".cue"));
    cues.forEach(function(cue) {
      if (cue._wired) return;
      cue._wired = true;
      var start = parseFloat(cue.getAttribute("data-start"));
      var seek = function() {
        if (!isNaN(start)) { media.currentTime = start; media.play(); }
      };
      cue.addEventListener("click", seek);
      cue.addEventListener("keydown", function(e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); seek(); }
      });
    });
    if (media._cueHighlightWired) return;
    media._cueHighlightWired = true;
    var active = null;
    media.addEventListener("timeupdate", function() {
      var all = document.querySelectorAll(".cue");
      var t = media.currentTime, current = null;
      for (var i = 0; i < all.length; i++) {
        if (parseFloat(all[i].getAttribute("data-start")) <= t) current = all[i];
        else break;
      }
      if (current !== active) {
        if (active) active.classList.remove("active");
        if (current) current.classList.add("active");
        active = current;
        if (current) maybeAutoscroll(current);
      }
    });
  }

  var media = document.querySelector("video, audio");
  wireCues(media);

  // --- Deep links (vts-5yyo) ---
  // A citation is only a citation if following it lands on the passage. The
  // seeking, highlighting and autoscrolling above already existed (VOS-111);
  // what was missing was ADDRESSING — a way to arrive here already positioned,
  // and a way to mark WHICH fragment was cited rather than which one happens
  // to be playing.
  //
  // Both ?t=12.5 and #t=12.5 are read: a query string survives copy-paste
  // through more tools, while a fragment never reaches the server, which is
  // the better default for a link that names a moment in someone's recording.
  function urlParam(name) {
    try {
      var value = new URLSearchParams(window.location.search).get(name);
      if (value !== null) return value;
      if (window.location.hash) {
        var m = window.location.hash.match(new RegExp("(?:^#|&)" + name + "=([^&]+)"));
        if (m) return decodeURIComponent(m[1]);
      }
    } catch (e) {
      return null;
    }
    return null;
  }

  function citedTarget() {
    var raw = urlParam("t");
    if (raw === null || raw === "") return null;
    var seconds = parseFloat(raw);
    // A malformed ?t=abc leaves the player alone rather than seeking to 0:00 —
    // jumping to the start would look like the link worked and lie about where
    // the citation pointed.
    return isNaN(seconds) ? null : Math.max(0, seconds);
  }

  // The cue whose window contains `seconds`, i.e. the sentence a citation at
  // that moment refers to.
  function cueAt(seconds) {
    var all = document.querySelectorAll(".cue");
    var found = null;
    for (var i = 0; i < all.length; i++) {
      var start = parseFloat(all[i].getAttribute("data-start"));
      if (!isNaN(start) && start <= seconds + 0.001) found = all[i];
      else break;
    }
    return found;
  }

  // "cited" is deliberately NOT "active": they mean different things and can
  // sit on different sentences at once — one is what the link pointed at, the
  // other is what is playing now. Reusing .active would make the citation
  // vanish the moment playback moved on.
  function markCited(cue) {
    var previous = document.querySelector(".cue.cited");
    if (previous) previous.classList.remove("cited");
    if (cue) cue.classList.add("cited");
  }

  function applyDeepLink() {
    // ?cue= addresses a sentence directly. Preferred over a timecode where
    // both are given: a time can drift if the transcript is re-rendered, while
    // the cue a citation named is the passage it actually quoted.
    var cueParam = urlParam("cue");
    var seconds = citedTarget();
    var cue = null;
    if (cueParam !== null && cueParam !== "") {
      cue = document.querySelector('.cue[data-cue="' + String(cueParam).replace(/["\\]/g, "") + '"]');
      if (cue && seconds === null) {
        var cueStart = parseFloat(cue.getAttribute("data-start"));
        if (!isNaN(cueStart)) seconds = cueStart;
      }
    }
    if (cue === null && seconds === null) return;
    if (cue === null) cue = cueAt(seconds);
    if (cue) {
      markCited(cue);
      scrollCueToCenter(cue);
    }
    if (media && !isNaN(seconds)) {
      // Seek without autoplaying: arriving at someone's recording should not
      // start sound the reader did not ask for.
      var seek = function() { try { media.currentTime = seconds; } catch (e) {} };
      if (media.readyState > 0) seek();
      else media.addEventListener("loadedmetadata", seek, { once: true });
    }
  }

  // --- Autoscroll (vts-eho) ---
  // The checkbox renders whenever media is present, even before the
  // transcript exists (task still processing -> blocks=[] on first paint).
  // ".transcript" itself may not exist yet at load time, so scrollBox starts
  // null and maybeAutoscroll/scrollCueToCenter re-check it live. Once the
  // transcript streams in via SSE, rebuildTranscript() calls wireAutoscroll()
  // again to (re)acquire ".transcript" and attach the scroll listener,
  // guarded by _autoscrollWired so it's never double-bound.
  var scrollBox = document.querySelector(".transcript");
  var autoToggle = document.getElementById("autoscroll-toggle");
  var programmaticScroll = false;
  var programmaticScrollTimer = null;

  function scrollCueToCenter(cue) {
    if (!cue || !scrollBox) return;
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

  function maybeAutoscroll(cue) {
    if (autoToggle && autoToggle.checked) scrollCueToCenter(cue);
  }

  function wireAutoscroll() {
    scrollBox = document.querySelector(".transcript");
    if (!scrollBox || scrollBox._autoscrollWired) return;
    scrollBox._autoscrollWired = true;
    scrollBox.addEventListener("scroll", function() {
      // Our own smooth-scroll fires scroll events too; ignore those.
      if (programmaticScroll) return;
      // A genuine user scroll turns autoscroll off.
      if (autoToggle && autoToggle.checked) autoToggle.checked = false;
    });
  }

  if (autoToggle) {
    autoToggle.addEventListener("change", function() {
      // Re-enabling brings the current sentence back into view.
      if (autoToggle.checked) {
        var cur = document.querySelector(".cue.active");
        if (cur) scrollCueToCenter(cur);
      }
    });
  }

  wireAutoscroll();
  // After wireAutoscroll, so scrollCueToCenter has its scroll box. Exposed for
  // the live-rebuild path: a transcript that streams in after load must still
  // honour the link that brought the reader here.
  window.__vtsApplyDeepLink = applyDeepLink;
  applyDeepLink();
/*__LIVE_SCRIPT__*/
})();

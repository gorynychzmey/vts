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
/*__LIVE_SCRIPT__*/
})();

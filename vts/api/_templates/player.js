(function() {
  // The /player page's adapter around VtsPlayerCore (player_core.js, inlined
  // before this script): the language, the transcript embedded by the server,
  // and the deep link in the URL. The live updates (player_live.js) are
  // spliced in below when the page knows its task.

  // Which language to show. The user's CHOICE in the app comes first: this page
  // has no access to app.js i18n, so it read navigator.languages alone — and a
  // German browser showed German to someone who had set the app to Russian.
  // Same origin, so the stored preference is readable here.
  function preferredLangs() {
    var out = [];
    try {
      var chosen = localStorage.getItem("vts_locale");
      if (chosen) out.push(chosen);
    } catch (e) { /* private mode: fall through to the browser */ }
    var browser = navigator.languages || [navigator.language || "en"];
    for (var i = 0; i < browser.length; i++) out.push(browser[i]);
    return out;
  }

  // The first translation matching the preference order, else English.
  function localizedMsg(map) {
    var langs = preferredLangs();
    for (var i = 0; i < langs.length; i++) {
      var code = String(langs[i] || "").slice(0, 2).toLowerCase();
      if (map[code]) return map[code];
    }
    return map.en || "";
  }

  function localize(el, attr) {
    if (!el) return;
    try {
      var text = localizedMsg(JSON.parse(el.getAttribute(attr) || "{}"));
      if (text) el.textContent = text;
    } catch (e) { /* keep the default English text */ }
  }

  localize(document.querySelector("[data-media-unavailable]"), "data-msgs");
  localize(document.querySelector("[data-autoscroll-label]"), "data-msgs");

  function initialBlocks() {
    var el = document.getElementById("vts-player-blocks");
    if (!el) return [];
    try { return JSON.parse(el.textContent || "[]"); } catch (e) { return []; }
  }

  var media = document.querySelector("video, audio");
  // No media, no transcript: with nothing to seek against the page shows only
  // the unavailable message.
  var player = media ? VtsPlayerCore.create({
    container: document.body,
    media: media,
    autoscrollToggle: document.getElementById("autoscroll-toggle"),
  }) : null;
  if (player) player.render(initialBlocks());

  // --- Deep links (vts-5yyo) ---
  // A citation is only a citation if following it lands on the passage.
  // Both ?t=12.5 and #t=12.5 are read: a query string survives copy-paste
  // through more tools, while a fragment never reaches the server, which is
  // the better default for a link that names a moment in someone's recording.
  // ?cue= addresses a sentence directly and wins over a time.
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

  function deepLinkTarget() {
    var raw = urlParam("t");
    // A malformed ?t=abc leaves the player alone rather than seeking to 0:00 —
    // jumping to the start would look like the link worked and lie about where
    // the citation pointed.
    var seconds = raw === null || raw === "" ? NaN : parseFloat(raw);
    return { seconds: isNaN(seconds) ? null : seconds, cue: urlParam("cue") };
  }

  if (player) player.showCited(deepLinkTarget());
/*__LIVE_SCRIPT__*/
})();

  // Live updates for a /player page that knows its task. Spliced into
  // player.js, so `player` and `localizedMsg` come from there.
  var TASK_ID = __TASK_ID__;
  var AS_USER = __AS_USER__;
  var MEDIA_MSGS = __MEDIA_MSGS__;

  function showMediaUnavailable() {
    var m = document.querySelector("video, audio");
    if (m) m.remove();
    var ol = document.querySelector(".transcript");
    if (ol) ol.remove();
    player = null;
    if (document.querySelector("[data-media-unavailable]")) return;
    var p = document.createElement("p");
    p.className = "media-unavailable";
    p.setAttribute("data-media-unavailable", "");
    p.textContent = localizedMsg(MEDIA_MSGS);
    document.body.appendChild(p);
  }

  function refetchEntries() {
    if (!player) return;
    var url = "/api/tasks/" + encodeURIComponent(TASK_ID) + "/transcript-entries";
    if (AS_USER) url += "?as_user=" + encodeURIComponent(AS_USER);
    fetch(url, { credentials: "same-origin" })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) { if (player && data && data.blocks) player.render(data.blocks); })
      .catch(function() { /* transient; next event or reload recovers */ });
  }

  try {
    var es = new EventSource("/api/events", { withCredentials: false });
    es.addEventListener("transcript_updated", function(ev) {
      try {
        var p = JSON.parse(ev.data);
        if (String(p.task_id) === TASK_ID) refetchEntries();
      } catch (e) {}
    });
    es.addEventListener("task_status", function(ev) {
      try {
        var p = JSON.parse(ev.data);
        if (String(p.task_id) !== TASK_ID) return;
        var status = String((p.data && p.data.status) || "");
        if (status === "canceled" || status === "archived" || status === "deleted") {
          showMediaUnavailable();
        }
      } catch (e) {}
    });
  } catch (e) { /* no SSE: page still works statically */ }

  if (media) {
    media.addEventListener("error", function() { showMediaUnavailable(); });
  }

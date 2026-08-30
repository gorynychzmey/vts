  var TASK_ID = __TASK_ID__;
  var AS_USER = __AS_USER__;
  var MEDIA_MSGS = __MEDIA_MSGS__;

  function localizedMsg(map) {
    var langs = (navigator.languages || [navigator.language || "en"]);
    for (var i = 0; i < langs.length; i++) {
      var code = String(langs[i] || "").slice(0, 2).toLowerCase();
      if (map[code]) return map[code];
    }
    return map.en || "";
  }

  function showMediaUnavailable() {
    var container = document.body;
    var m = document.querySelector("video, audio");
    if (m) m.remove();
    var ol = document.querySelector(".transcript");
    if (ol) ol.remove();
    if (document.querySelector("[data-media-unavailable]")) return;
    var p = document.createElement("p");
    p.className = "media-unavailable";
    p.setAttribute("data-media-unavailable", "");
    p.textContent = localizedMsg(MEDIA_MSGS);
    container.appendChild(p);
  }

  function timecode(start) {
    var s = Math.max(0, Math.floor(Number(start) || 0));
    var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh
      ? hh + ":" + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0")
      : mm + ":" + String(ss).padStart(2, "0");
  }

  function buildCue(sentence) {
    var span = document.createElement("span");
    span.className = "cue";
    span.setAttribute("data-start", String(sentence.start));
    span.setAttribute("role", "button");
    span.setAttribute("tabindex", "0");
    span.title = timecode(sentence.start);
    span.textContent = String(sentence.text || "");
    return span;
  }

  var cueCounter = 0;

  function buildBlock(block) {
    var li = document.createElement("li");
    li.className = "block";
    if (block.label) {
      var lab = document.createElement("div");
      lab.className = "block-label";
      lab.textContent = String(block.label);
      li.appendChild(lab);
    }
    var body = document.createElement("p");
    body.className = "block-body";
    (block.sentences || []).forEach(function(sentence, i) {
      if (i) body.appendChild(document.createTextNode(" "));
      // Numbered across the WHOLE transcript, exactly as the server renders it
      // — a deep link addresses a sentence in the recording, and the two paths
      // must agree or a citation breaks the moment the transcript rebuilds.
      var cue = buildCue(sentence);
      cue.setAttribute("data-cue", String(cueCounter++));
      body.appendChild(cue);
    });
    li.appendChild(body);
    return li;
  }

  function rebuildTranscript(blocks) {
    var media = document.querySelector("video, audio");
    if (!media || !Array.isArray(blocks) || !blocks.length) return;
    var ol = document.querySelector(".transcript");
    if (!ol) {
      ol = document.createElement("ol");
      ol.className = "transcript";
      document.body.appendChild(ol);
    }
    ol.innerHTML = "";
    cueCounter = 0;
    blocks.forEach(function(block) { ol.appendChild(buildBlock(block)); });
    wireCues(media);
    wireAutoscroll();
  }

  function refetchEntries() {
    var url = "/api/tasks/" + encodeURIComponent(TASK_ID) + "/transcript-entries";
    if (AS_USER) url += "?as_user=" + encodeURIComponent(AS_USER);
    fetch(url, { credentials: "same-origin" })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(data) { if (data && data.blocks) rebuildTranscript(data.blocks); })
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

  var mediaEl = document.querySelector("video, audio");
  if (mediaEl) {
    mediaEl.addEventListener("error", function() { showMediaUnavailable(); });
  }

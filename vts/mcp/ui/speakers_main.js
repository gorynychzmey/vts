(function() {
  // Entry point of ui://vts/speakers: a recording's voices, each to listen to
  // and say who it is. The view sends plain choices; app_resolve_speakers
  // turns them into the web dialog's resolutions on the server and saves them
  // through the same service, so the two UIs cannot disagree on what a choice
  // means.
  var MSGS = {
    en: {
      untitled: "Untitled recording", listen: "▶ Listen", noClip: "No clip for this voice.",
      newPerson: "New person…", anonymous: "Anonymous", namePlaceholder: "Name",
      noise: "Noise", remember: "Remember the voice", save: "Save", saveContinue: "Save and continue processing",
      saving: "Saving…", saved: "Saved.", failed: "Could not save.", loadFailed: "The voices could not be loaded.",
      now: "now", notIdentified: "not identified", isNoise: "noise",
      task_deleted: "The task that produced this recording was deleted; its voices can no longer be changed.",
      not_diarized: "This recording was not processed with speaker separation.",
      readOnly: "The voices cannot be changed right now.",
      nameNeeded: "Give the new person a name, or choose Anonymous.",
    },
    ru: {
      untitled: "Запись без названия", listen: "▶ Послушать", noClip: "Для этого голоса нет фрагмента.",
      newPerson: "Новый человек…", anonymous: "Аноним", namePlaceholder: "Имя",
      noise: "Шум", remember: "Запомнить голос", save: "Сохранить", saveContinue: "Сохранить и продолжить обработку",
      saving: "Сохраняю…", saved: "Сохранено.", failed: "Не удалось сохранить.", loadFailed: "Не удалось загрузить голоса.",
      now: "сейчас", notIdentified: "не опознан", isNoise: "шум",
      task_deleted: "Задача, создавшая эту запись, удалена; голоса больше нельзя изменить.",
      not_diarized: "Эта запись обработана без разделения по спикерам.",
      readOnly: "Сейчас голоса изменить нельзя.",
      nameNeeded: "Дайте новому человеку имя или выберите «Аноним».",
    },
    de: {
      untitled: "Aufnahme ohne Titel", listen: "▶ Anhören", noClip: "Für diese Stimme gibt es keinen Ausschnitt.",
      newPerson: "Neue Person…", anonymous: "Anonym", namePlaceholder: "Name",
      noise: "Rauschen", remember: "Stimme merken", save: "Speichern", saveContinue: "Speichern und weiterverarbeiten",
      saving: "Wird gespeichert…", saved: "Gespeichert.", failed: "Speichern fehlgeschlagen.", loadFailed: "Die Stimmen konnten nicht geladen werden.",
      now: "jetzt", notIdentified: "nicht erkannt", isNoise: "Rauschen",
      task_deleted: "Die Aufgabe dieser Aufnahme wurde gelöscht; die Stimmen lassen sich nicht mehr ändern.",
      not_diarized: "Diese Aufnahme wurde ohne Sprechertrennung verarbeitet.",
      readOnly: "Die Stimmen lassen sich gerade nicht ändern.",
      nameNeeded: "Geben Sie der neuen Person einen Namen oder wählen Sie „Anonym“.",
    },
  };
  var NEW = "__new__";
  var ANON = "__anonymous__";
  // For the model, not the user: always English.
  var SAVED = "In the VTS speakers view the user saved who the voices of recording";

  var bridge = VtsAppBridge.connect({ name: "vts-speakers", version: "1" });
  var msgs = MSGS.en;
  var data = null;
  var rows = [];
  var saving = false;

  function $(attr) { return document.querySelector("[" + attr + "]"); }
  function setStatus(text) { $("data-status").textContent = text || ""; }

  function applyHostContext() {
    var ctx = bridge.hostContext || {};
    var lang = String(ctx.locale || navigator.language || "en").slice(0, 2).toLowerCase();
    msgs = MSGS[lang] || MSGS.en;
    if (ctx.theme) document.documentElement.setAttribute("data-theme", ctx.theme);
    var vars = ctx.styles && ctx.styles.variables;
    if (vars) for (var k in vars) if (k.indexOf("--") === 0) document.documentElement.style.setProperty(k, vars[k]);
    $("data-save").textContent = msgs.save;
    $("data-save-continue").textContent = msgs.saveContinue;
  }

  function reportSize() {
    bridge.reportSize(document.documentElement.scrollWidth, document.documentElement.scrollHeight);
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  // The initial choice, as the web dialog picks it: a saved decision wins; a
  // saved "anonymous" stays anonymous; otherwise the matcher's best guess
  // unless it found nobody.
  function initialChoice(v) {
    var ids = v.options.map(function(o) { return o.speaker_id; });
    if (v.decided_speaker_id && ids.indexOf(String(v.decided_speaker_id)) >= 0) return String(v.decided_speaker_id);
    if (v.decided_is_noise !== null && v.decided_is_noise !== undefined) return ANON;
    if (v.outcome !== "miss" && v.options.length) return v.options[0].speaker_id;
    return ANON;
  }

  function currentText(v) {
    if (v.decided_is_noise) return msgs.isNoise;
    return v.decided_name || msgs.notIdentified;
  }

  async function listen(v, holder, button) {
    button.disabled = true;
    try {
      var res = await bridge.callTool("app_speaker_preview_url", { recording_id: data.recording_id, label: v.label, index: 0 });
      var link = res.structuredContent || {};
      if (!link.url) throw new Error("no clip");
      var audio = document.createElement("audio");
      audio.controls = true;
      audio.src = link.url;
      holder.innerHTML = "";
      holder.appendChild(audio);
      var p = audio.play();
      if (p && p.catch) p.catch(function() {});
    } catch (e) {
      holder.textContent = msgs.noClip;
    }
    button.disabled = false;
  }

  function voiceItem(v, editable) {
    var row = { voice: v, choice: initialChoice(v), name: "", noise: !!v.decided_is_noise,
      remember: v.outcome !== "miss" };
    var li = document.createElement("li");
    li.className = "voice";
    li.setAttribute("data-voice", v.label);
    var head = document.createElement("div");
    head.className = "voice-head";
    var label = document.createElement("span");
    label.className = "voice-label";
    label.textContent = v.display_label;
    var now = document.createElement("span");
    now.className = "voice-now";
    now.setAttribute("data-now", "");
    now.textContent = msgs.now + ": " + currentText(v);
    head.appendChild(label);
    head.appendChild(now);
    li.appendChild(head);

    var controls = document.createElement("div");
    controls.className = "voice-controls";
    var holder = document.createElement("div");
    holder.setAttribute("data-clip", "");
    if (v.clips > 0) {
      var listenBtn = document.createElement("button");
      listenBtn.type = "button";
      listenBtn.className = "more";
      listenBtn.setAttribute("data-listen", "");
      listenBtn.textContent = msgs.listen;
      listenBtn.addEventListener("click", function() { listen(v, holder, listenBtn); });
      controls.appendChild(listenBtn);
    }
    if (editable) {
      var select = document.createElement("select");
      select.setAttribute("data-choice", "");
      v.options.forEach(function(o) {
        var opt = document.createElement("option");
        opt.value = o.speaker_id;
        opt.textContent = o.name;
        select.appendChild(opt);
      });
      [[NEW, msgs.newPerson], [ANON, msgs.anonymous]].forEach(function(pair) {
        var opt = document.createElement("option");
        opt.value = pair[0];
        opt.textContent = pair[1];
        select.appendChild(opt);
      });
      select.value = row.choice;
      var name = document.createElement("input");
      name.type = "text";
      name.placeholder = msgs.namePlaceholder;
      name.setAttribute("data-new-name", "");
      name.hidden = row.choice !== NEW;
      var remember = document.createElement("label");
      var rememberBox = document.createElement("input");
      rememberBox.type = "checkbox";
      rememberBox.checked = row.remember;
      rememberBox.setAttribute("data-remember", "");
      remember.appendChild(rememberBox);
      remember.appendChild(document.createTextNode(msgs.remember));
      remember.hidden = row.choice === NEW || row.choice === ANON;
      var noise = document.createElement("label");
      var noiseBox = document.createElement("input");
      noiseBox.type = "checkbox";
      noiseBox.checked = row.noise;
      noiseBox.setAttribute("data-noise", "");
      noise.appendChild(noiseBox);
      noise.appendChild(document.createTextNode(msgs.noise));
      select.addEventListener("change", function() {
        row.choice = select.value;
        name.hidden = row.choice !== NEW;
        remember.hidden = row.choice === NEW || row.choice === ANON;
      });
      name.addEventListener("input", function() { row.name = name.value; });
      rememberBox.addEventListener("change", function() { row.remember = rememberBox.checked; });
      noiseBox.addEventListener("change", function() { row.noise = noiseBox.checked; });
      controls.appendChild(select);
      controls.appendChild(name);
      controls.appendChild(remember);
      controls.appendChild(noise);
    }
    li.appendChild(controls);
    li.appendChild(holder);
    rows.push(row);
    return li;
  }

  function show(next) {
    data = next || {};
    rows = [];
    $("data-title").textContent = data.title || msgs.untitled;
    var list = $("data-voices");
    list.innerHTML = "";
    var editable = !!data.editable;
    (data.voices || []).forEach(function(v) { list.appendChild(voiceItem(v, editable)); });
    var note = "";
    if (!editable) note = msgs[data.reason] || msgs.readOnly;
    $("data-note").textContent = note;
    $("data-actions").hidden = !editable || !(data.voices || []).length;
    $("data-save-continue").hidden = !data.awaiting_input;
  }

  function choices() {
    var out = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var c = { is_noise: r.noise };
      if (r.choice === NEW) {
        if (!r.name.trim()) return null;
        c.kind = "new"; c.new_name = r.name.trim();
      } else if (r.choice === ANON) {
        c.kind = "anonymous";
      } else {
        c.kind = "person"; c.speaker_id = r.choice; c.add_fragment = r.remember;
      }
      out[r.voice.label] = c;
    }
    return out;
  }

  async function save(continueTask) {
    // One save at a time: a second click while the first is on its way would
    // only replay it (the server serialises them anyway).
    if (saving) return;
    var voices = choices();
    if (!voices) { setStatus(msgs.nameNeeded); return; }
    saving = true;
    $("data-save").disabled = true;
    $("data-save-continue").disabled = true;
    setStatus(msgs.saving);
    try {
      await bridge.callTool("app_resolve_speakers", {
        recording_id: data.recording_id, voices: voices, continue_task: !!continueTask,
      });
      var fresh = await bridge.callTool("show_speakers", { recording_id: data.recording_id });
      show(fresh.structuredContent || {});
      setStatus(msgs.saved);
      var summary = (data.voices || []).map(function(v) {
        return v.display_label + " = " + (v.decided_is_noise ? "noise" : (v.decided_name || "anonymous"));
      }).join(", ");
      bridge.updateModelContext(SAVED + " " + data.recording_id + " are: " + summary + ".");
    } catch (e) {
      setStatus(e.message || msgs.failed);
    }
    saving = false;
    $("data-save").disabled = false;
    $("data-save-continue").disabled = false;
  }

  $("data-save").addEventListener("click", function() { save(false); });
  $("data-save-continue").addEventListener("click", function() { save(true); });

  bridge.on("tool-result", function(result) {
    if (result.isError) {
      setStatus(((result.content || [])[0] || {}).text || msgs.loadFailed);
      return;
    }
    show(result.structuredContent || {});
  });
  bridge.on("host-context", applyHostContext);
  bridge.ready.then(applyHostContext).catch(function() { setStatus(msgs.loadFailed); });
})();

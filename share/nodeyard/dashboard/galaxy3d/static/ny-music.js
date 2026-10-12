/* Keep the Nodeyard Music player available when the full-page AI Galaxy is open. */
(function () {
  "use strict";
  var state = null, audio = null, bar = null, title = null, playButton = null, repeatButton = null, repeatIcon = null, repeatMode = null;
  var shuffleOrder = null, shufflePos = 0;
  var historySince = 0, historyElapsed = 0, lastHistorySaved = 0, lastPlaybackSaved = 0, saving = false;
  function api(path, body, keepalive) {
    var opts = { method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store" };
    if (body) { opts.headers = { "Content-Type": "application/json", "X-Nodeyard": "1" }; opts.body = JSON.stringify(body); }
    if (keepalive) opts.keepalive = true;
    return fetch(path, opts).then(function (r) { return r.json(); });
  }
  function src(track) {
    return track.url ? "/api/music/sc-stream?url=" + encodeURIComponent(track.url) : "/api/music/track?path=" + encodeURIComponent(track.path);
  }
  function historyTrack(track) { return track.url ? Object.assign({}, track, { kind: "sc" }) : Object.assign({}, track, { kind: "local" }); }
  function beginHistory() {
    if (!state || !state.queue[state.index]) return;
    if (!state.history_id) {
      state.history_id = window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
      historyElapsed = 0;
      api("/api/music/history", { action: "start", id: state.history_id, seconds: 0, track: historyTrack(state.queue[state.index]) }).catch(function () {});
    }
    if (!historySince) historySince = performance.now();
  }
  function syncHistory(force) {
    if (!state || !state.history_id || !state.queue[state.index]) return;
    if (historySince) {
      historyElapsed += Math.max(0, (performance.now() - historySince) / 1000);
      historySince = audio && !audio.paused ? performance.now() : 0;
    }
    if (!force && Date.now() - lastHistorySaved < 15000) return;
    lastHistorySaved = Date.now();
    api("/api/music/history", { action: "progress", id: state.history_id, seconds: Math.floor(historyElapsed),
      track: historyTrack(state.queue[state.index]) }, force).catch(function () {});
  }
  function playbackPayload(handoff) {
    return { queue: state.queue, index: state.index, position: audio.currentTime || 0, volume: audio.volume,
      rate: audio.playbackRate || state.rate || 1,
      playing: !audio.paused, shuffle: !!state.shuffle, repeat: state.repeat || "off", history_id: state.history_id || "",
      history_seconds: Math.floor(historyElapsed + (historySince && !audio.paused ? (performance.now() - historySince) / 1000 : 0)), handoff: !!handoff };
  }
  function save(handoff, keepalive) {
    if (!state || !audio || !state.queue.length) return;
    if (!keepalive && Date.now() - lastPlaybackSaved < 15000) return;
    lastPlaybackSaved = Date.now();
    if (saving && !keepalive) return;
    saving = true;
    api("/api/music/playback", playbackPayload(handoff), keepalive).catch(function () {}).finally(function () { saving = false; });
  }
  function paint() {
    if (!bar || !state) return;
    var t = state.queue[state.index];
    title.textContent = t ? ((t.title || t.name || "Untitled") + (t.artist ? " · " + t.artist : "")) : "Music";
    playButton.textContent = audio.paused ? "▶" : "⏸";
    var label = state.repeat === "one" ? "Song" : state.repeat === "all" ? "Playlist" : "Off";
    repeatIcon.textContent = state.repeat === "one" ? "🔂" : "🔁";
    repeatMode.textContent = label;
    repeatButton.classList.toggle("on", state.repeat !== "off");
    repeatButton.title = "Loop: " + label + " (click to change)";
    repeatButton.setAttribute("aria-label", repeatButton.title);
    repeatButton.setAttribute("aria-pressed", state.repeat === "off" ? "false" : "true");
  }
  function resetShuffle(exclude) {
    shuffleOrder = null; shufflePos = 0;
    if (!state || !state.shuffle || state.queue.length < 2) return;
    shuffleOrder = state.queue.map(function (_track, i) { return i; }).filter(function (i) { return i !== exclude; });
    for (var i = shuffleOrder.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1)), swap = shuffleOrder[i]; shuffleOrder[i] = shuffleOrder[j]; shuffleOrder[j] = swap;
    }
  }
  function makeBar() {
    var css = document.createElement("style");
    css.textContent = "#ny-music{position:fixed;right:14px;bottom:14px;z-index:10001;display:flex;align-items:center;gap:7px;max-width:calc(100vw - 28px);padding:7px 10px;border:1px solid rgba(100,175,255,.3);border-radius:12px;background:rgba(5,10,22,.86);box-shadow:0 8px 28px rgba(0,0,0,.35);backdrop-filter:blur(10px);color:#e5efff;font:12px ui-monospace,Menlo,monospace}" +
      "#ny-music[hidden]{display:none}#ny-music .nt{max-width:min(34vw,360px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#afc7ef}" +
      "#ny-music button,#ny-music a{font:inherit;color:inherit;background:rgba(120,170,255,.1);border:1px solid rgba(120,170,255,.22);border-radius:7px;padding:5px 7px;text-decoration:none;cursor:pointer}" +
      "#ny-music button:hover,#ny-music a:hover,#ny-music button.on{background:rgba(90,215,255,.24);color:white}" +
      "#ny-music .loop-mode{font-size:10px;font-weight:700;opacity:.85;margin-left:3px}" +
      "@media(max-width:560px){#ny-music{left:8px;right:8px;bottom:8px;justify-content:center}#ny-music .nt{max-width:36vw}}";
    document.head.appendChild(css);
    bar = document.createElement("div"); bar.id = "ny-music"; bar.hidden = true;
    bar.innerHTML = '<button data-music="prev" title="Previous">⏮</button><button data-music="play" title="Play or pause">▶</button><button data-music="next" title="Next">⏭</button><button data-music="repeat" title="Loop: Off (click to change)" aria-label="Loop: Off (click to change)" aria-pressed="false"><span class="loop-icon">🔁</span><span class="loop-mode">Off</span></button><span class="nt"></span><a href="/">Music</a>';
    document.body.appendChild(bar);
    title = bar.querySelector(".nt"); playButton = bar.querySelector('[data-music="play"]'); repeatButton = bar.querySelector('[data-music="repeat"]');
    repeatIcon = repeatButton.querySelector(".loop-icon"); repeatMode = repeatButton.querySelector(".loop-mode");
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("[data-music]"); if (!b || !state) return;
      if (b.dataset.music === "play") { if (audio.paused) audio.play().catch(function () {}); else audio.pause(); }
      else if (b.dataset.music === "repeat") { state.repeat = state.repeat === "off" ? "one" : state.repeat === "one" ? "all" : "off"; save(false, true); paint(); }
      else if (b.dataset.music === "prev") select(Math.max(0, state.index - 1));
      else if (b.dataset.music === "next") advance();
    });
  }
  function select(i) {
    if (!state || i < 0 || i >= state.queue.length) return;
    syncHistory(true); state.history_id = ""; historyElapsed = 0; historySince = 0;
    state.index = i; if (state.shuffle) resetShuffle(i); audio.src = src(state.queue[i]); audio.currentTime = 0;
    audio.play().then(function () { beginHistory(); save(false, true); }).catch(function () {});
    paint(); save(false, true);
  }
  function advance() {
    if (!state || !state.queue.length) return;
    var i = state.index + 1;
    if (state.shuffle && state.queue.length > 1) {
      if (!shuffleOrder) resetShuffle(state.index);
      if (shufflePos >= shuffleOrder.length) {
        if (state.repeat !== "all") { audio.pause(); save(false, true); return; }
        resetShuffle(state.index);
      }
      i = shuffleOrder[shufflePos++];
      selectShuffled(i);
      return;
    }
    if (i >= state.queue.length) i = state.repeat === "all" ? 0 : -1;
    if (i < 0) { audio.pause(); save(false, true); return; }
    select(i);
  }
  function selectShuffled(i) {
    if (!state || i < 0 || i >= state.queue.length) return;
    syncHistory(true); state.history_id = ""; historyElapsed = 0; historySince = 0;
    state.index = i; audio.src = src(state.queue[i]); audio.currentTime = 0;
    audio.play().then(function () { beginHistory(); save(false, true); }).catch(function () {});
    paint(); save(false, true);
  }
  function restore(saved) {
    if (!saved || !Array.isArray(saved.queue) || !saved.queue.length) return;
    state = saved; state.index = Math.max(0, Math.min(+saved.index || 0, state.queue.length - 1));
    resetShuffle(state.index);
    historyElapsed = Math.max(0, +saved.history_seconds || 0); historySince = 0; audio = new Audio(); audio.preload = "auto";
    audio.volume = Math.max(0, Math.min(1, Number.isFinite(+saved.volume) ? +saved.volume : 1));
    state.rate = Math.max(0.5, Math.min(2, Number.isFinite(+saved.rate) ? +saved.rate : 1));
    audio.defaultPlaybackRate = state.rate; audio.playbackRate = state.rate;
    audio.preservesPitch = true; audio.webkitPreservesPitch = true;
    audio.addEventListener("ratechange", function () {
      var rate = Math.max(0.5, Math.min(2, Number.isFinite(+audio.playbackRate) ? +audio.playbackRate : 1));
      if (Math.abs(rate - state.rate) > 0.001) { state.rate = rate; save(false, true); }
    });
    audio.src = src(state.queue[state.index]);
    var position = Math.max(0, +saved.position || 0);
    audio.addEventListener("loadedmetadata", function () { if (position < audio.duration) audio.currentTime = position; }, { once: true });
    audio.addEventListener("play", paint); audio.addEventListener("pause", function () { syncHistory(true); paint(); save(false, true); });
    audio.addEventListener("playing", function () { beginHistory(); paint(); });
    audio.addEventListener("timeupdate", function () { syncHistory(false); save(false, false); });
    audio.addEventListener("ended", function () {
      syncHistory(true);
      if (state.history_id) api("/api/music/history", { action: "complete", id: state.history_id, seconds: Math.floor(historyElapsed), track: historyTrack(state.queue[state.index]) }).catch(function () {});
      if (state.repeat === "one") select(state.index); else advance();
    });
    makeBar(); bar.hidden = false; paint();
    if (saved.playing) audio.play().then(function () { save(false, true); }).catch(function () { title.textContent += " · press play to resume"; });
    save(false, true);
  }
  fetch("/api/music/playback", { cache: "no-store", credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (data) {
    var p = data.playback || {}, age = Date.now() / 1000 - (+p.updated || 0);
    // A SwiftUI section change can destroy the Music WKWebView without firing pagehide.
    // Recent saved state is enough; `playing` decides whether the Galaxy player should resume.
    if (data.ok && age >= 0 && age < 180) restore(p);
  }).catch(function () {});
  window.addEventListener("pagehide", function () { syncHistory(true); save(true, true); });
})();

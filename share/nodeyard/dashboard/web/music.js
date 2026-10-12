// The Music page — a small SoundCloud-style player — and the persistent player bar.
// The server streams a folder of music (with seeking), streams SoundCloud tracks as MP3 (so they play in the browser),
// and, when the admin turns it on, forwards the host's own audio output as live MP3. A browser only needs the dashboard.
// Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, toast, getJSON, postJSON } = U;

  function readDJState() {
    const initial = { active: false, target: "", voice: "", speak: true, messages: [], draft: null, targets: null, busy: false };
    try {
      const saved = JSON.parse(localStorage.getItem("ny-music-dj") || "{}");
      if (!saved || typeof saved !== "object") return initial;
      return Object.assign(initial, saved, {
        messages: Array.isArray(saved.messages) ? saved.messages.slice(-16).filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string") : [],
        draft: saved.draft && Array.isArray(saved.draft.tracks) ? saved.draft : null,
        targets: null, busy: false,
      });
    } catch (e) { return initial; }
  }

  const M = {
    data: null, tab: "search",
    queue: [], idx: -1, live: false, seekable: true, shuffle: false, shuffleOrder: null, shufflePos: 0, repeat: "off",
    scResults: [], scSet: null, scLikes: [], playlists: [], openPl: null, addTrack: null,
    dj: readDJState(),
    historyId: "", historyElapsed: 0, historySince: 0, lastHistorySave: 0, lastPlaybackSave: 0, restored: false, stopping: false,
    hideDrm: (() => { try { return localStorage.getItem("ny-hidedrm") === "1"; } catch (e) { return false; } })(),
  };
  let audio = null, bar = null;
  let actx = null, analyser = null, vizData = null, vizSource = null, vizRAF = 0;

  // ---- the persistent player bar ------------------------------------------
  function ensureBar() {
    if (bar) return;
    const style = document.createElement("style");
    style.textContent =
      "#ny-player{position:fixed;left:0;right:0;bottom:0;z-index:60;display:none;align-items:center;gap:12px;" +
      "padding:9px 16px;background:var(--card,#131a2e);border-top:1px solid var(--line,rgba(125,160,200,.18));" +
      "box-shadow:0 -8px 28px rgba(0,0,0,.35);font-size:13px}" +
      "#ny-player.on{display:flex}" +
      "#ny-player .np{min-width:150px;max-width:230px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}" +
      "#ny-player .np b{display:block;overflow:hidden;text-overflow:ellipsis}" +
      "#ny-player .np span{color:var(--ink3,#8fa3c0);font-size:11px}" +
      "#ny-player canvas{width:120px;height:30px;flex:none;opacity:.95}" +
      "#ny-player button{background:none;border:0;color:inherit;cursor:pointer;font-size:16px;padding:4px;border-radius:6px}" +
      "#ny-player button:hover{background:rgba(125,160,200,.14)}" +
      "#ny-player button:disabled{opacity:.4;cursor:default}" +
      "#ny-player button.act{color:var(--sc,#ff5500)}" +
      "#ny-player button.act::after{content:'';position:absolute;left:50%;bottom:1px;transform:translateX(-50%);width:4px;height:4px;border-radius:50%;background:var(--sc,#ff5500)}" +
      "#ny-player #ny-shuffle,#ny-player #ny-repeat{position:relative}" +
      "#ny-player .loop-state{font-size:10px;font-weight:700;letter-spacing:.03em;color:var(--ink3,#8fa3c0);min-width:46px}" +
      "#ny-player .loop-state.on{color:var(--sc,#ff5500)}" +
      "#ny-player .seek{flex:1;display:flex;align-items:center;gap:8px;min-width:110px}" +
      "#ny-player input[type=range]{flex:1;accent-color:var(--sc,#ff5500)}" +
      "#ny-player .t{font-variant-numeric:tabular-nums;color:var(--ink3,#8fa3c0);font-size:11px;min-width:38px}" +
      "#ny-player .live-pill{color:#ff5a6a;font-weight:600;font-size:11px;letter-spacing:1px}" +
      ".sc-tabs{display:inline-flex;gap:3px;flex-wrap:wrap;margin:6px 0 18px;padding:4px;background:rgba(125,160,200,.08);border:1px solid var(--line);border-radius:999px}" +
      ".sc-tabs button{background:none;border:0;color:var(--ink2,#b9c6dd);cursor:pointer;padding:7px 18px;border-radius:999px;font-size:14px;font-weight:600;transition:background .15s,color .15s,box-shadow .15s}" +
      ".sc-tabs button:hover{background:rgba(125,160,200,.15);color:var(--ink,#eaf1ff)}" +
      ".sc-tabs button.on{background:var(--sc,#ff5500);color:#fff;box-shadow:0 3px 10px rgba(255,85,0,.38)}" +
      ".sc-row{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;padding:8px 2px;border-bottom:1px solid var(--line)}" +
      ".sc-row .meta{flex:1;min-width:0;overflow:hidden}" +
      ".sc-row .btn,.sc-row a.btn{flex:none}" +
      ".sc-row .meta div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      ".sc-pbtn{flex:none;width:38px;height:38px;border-radius:50%;background:var(--sc,#ff5500);color:#fff;border:0;cursor:pointer;font-size:15px}" +
      ".sc-pbtn.ghost{background:rgba(125,160,200,.16);color:var(--ink,#eaf1ff)}" +
      ".ny-viz-top{display:block;width:100%;height:56px;margin:2px 0 14px;border-radius:12px;background:linear-gradient(180deg,rgba(255,85,0,.05),rgba(125,160,200,.06))}" +
      "@media (max-width:760px){#ny-player{bottom:56px}#ny-player .np{max-width:100px}#ny-player canvas{display:none}}";
    document.head.appendChild(style);
    // SoundCloud-ish accent available as --sc (falls back to orange).
    document.documentElement.style.setProperty("--sc", getComputedStyle(document.documentElement).getPropertyValue("--sc") || "#ff5500");
    bar = document.createElement("div");
    bar.id = "ny-player";
    bar.innerHTML =
      '<div class="np"><b id="ny-np-title">—</b><span id="ny-np-sub"></span></div>' +
      '<canvas id="ny-viz" width="120" height="30" aria-hidden="true"></canvas>' +
      '<button id="ny-prev" title="Previous">⏮</button>' +
      '<button id="ny-play" title="Play/pause">▶</button>' +
      '<button id="ny-next" title="Next">⏭</button>' +
      '<div class="seek"><span class="t" id="ny-cur">0:00</span><input type="range" id="ny-seek" min="0" max="100" value="0" step="0.1" aria-label="Seek"><span class="t" id="ny-dur">0:00</span></div>' +
      '<span class="live-pill" id="ny-live-pill" hidden>● LIVE</span>' +
      '<button id="ny-shuffle" title="Shuffle">🔀</button>' +
      '<button id="ny-repeat" title="Loop: Off (click to change)" aria-label="Loop: Off (click to change)" aria-pressed="false">🔁</button><span id="ny-repeat-state" class="loop-state">Off</span>' +
      '<button id="ny-copy" title="Copy SoundCloud link" aria-label="Copy SoundCloud link">⧉</button>' +
      '<button id="ny-vol-i" title="Volume">🔊</button><input type="range" id="ny-vol" min="0" max="1" value="1" step="0.05" style="width:70px" aria-label="Volume">' +
      '<button id="ny-close" title="Close player">✕</button>';
    document.body.appendChild(bar);
    audio = new Audio();
    audio.preload = "metadata";
    audio.crossOrigin = "anonymous";
    audio.defaultPlaybackRate = 1;
    audio.playbackRate = 1;
    audio.addEventListener("ratechange", () => {
      // There is no speed control in this player; keep unexpected browser rate changes from altering playback.
      if (audio.playbackRate !== 1) audio.playbackRate = 1;
    });
    audio.addEventListener("timeupdate", () => { tick(); historyProgress(false); savePlayback(false, false); });
    audio.addEventListener("durationchange", tick);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("play", () => { startViz(); paintBtn(); savePlayback(false, false); });
    audio.addEventListener("playing", () => {
      const t = M.queue[M.idx];
      if (t && !M.live) { $("#ny-np-sub").textContent = subOf(t); beginHistory(t); }
    });
    audio.addEventListener("pause", () => { historyProgress(true); paintBtn(); if (!M.stopping) savePlayback(false, true); });
    audio.addEventListener("error", onAudioError);
    $("#ny-play").onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
    $("#ny-prev").onclick = () => { if (!M.live) play(M.idx - 1); };
    $("#ny-next").onclick = () => { if (!M.live) next(); };
    $("#ny-close").onclick = stop;
    $("#ny-shuffle").onclick = () => { M.shuffle = !M.shuffle; resetShuffle(M.idx); paintBtn(); savePlayback(false, true); };
    $("#ny-repeat").onclick = () => { M.repeat = M.repeat === "off" ? "one" : M.repeat === "one" ? "all" : "off"; paintBtn(); savePlayback(false, true); };
    $("#ny-copy").onclick = copyCurrentLink;
    $("#ny-seek").oninput = (e) => { if (audio.duration && M.seekable && !M.live) audio.currentTime = (e.target.value / 100) * audio.duration; };
    $("#ny-vol").oninput = (e) => { audio.volume = +e.target.value; savePlayback(false, false); };
  }

  // ---- CAVA-style visualizer ----------------------------------------------
  function startViz() {
    try {
      if (!actx) {
        actx = new (window.AudioContext || window.webkitAudioContext)();
        vizSource = actx.createMediaElementSource(audio);
        analyser = actx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.8;
        vizSource.connect(analyser);
        analyser.connect(actx.destination);
        vizData = new Uint8Array(analyser.frequencyBinCount);
        // A routed element goes silent if the context suspends (tab switch, heavy WebGL like the 3D galaxy, browser
        // throttling), which stopped the music when moving around the dashboard. Resume whenever that happens.
        const keep = () => { if (actx.state === "suspended" && audio && !audio.paused) actx.resume().catch(() => {}); };
        actx.addEventListener("statechange", keep);
        document.addEventListener("visibilitychange", keep);
        window.addEventListener("focus", keep);
        setInterval(keep, 4000);
      }
      if (actx.state === "suspended") actx.resume();
      if (!vizRAF && !audio.paused) drawViz();
    } catch (e) { /* Web Audio unavailable — fine, sound still plays */ }
  }
  function drawBars(cv, bars) {
    const ctx = cv.getContext("2d"); const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const step = Math.floor(vizData.length / bars) || 1, bw = w / bars;
    const col = (getComputedStyle(document.documentElement).getPropertyValue("--sc") || "#ff5500").trim();
    for (let i = 0; i < bars; i++) {
      let v = 0; for (let j = 0; j < step; j++) v += vizData[i * step + j] || 0; v /= step;
      const bh = Math.max(2, (v / 255) * h);
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.3 + 0.7 * (v / 255);
      ctx.fillRect(i * bw + 1, h - bh, Math.max(1, bw - 2), bh);
    }
    ctx.globalAlpha = 1;
  }
  function drawViz() {
    vizRAF = requestAnimationFrame(drawViz);
    if (!analyser) return;
    const small = document.getElementById("ny-viz"), top = document.getElementById("sc-viz-top");
    if (audio.paused) {
      [small, top].forEach((cv) => { if (cv) cv.getContext("2d").clearRect(0, 0, cv.width, cv.height); });
      cancelAnimationFrame(vizRAF); vizRAF = 0; return;
    }
    analyser.getByteFrequencyData(vizData);
    if (small) drawBars(small, 28);
    if (top) drawBars(top, 56);
  }

  const fmtTime = (s) => (isFinite(s) && s >= 0 ? Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0") : "0:00");
  function tick() {
    if (!bar) return;
    $("#ny-cur").textContent = fmtTime(audio.currentTime);
    const known = M.idx >= 0 && M.queue[M.idx] && M.queue[M.idx].duration_ms ? M.queue[M.idx].duration_ms / 1000 : audio.duration;
    $("#ny-dur").textContent = M.live ? "live" : fmtTime(known);
    const sk = $("#ny-seek");
    sk.disabled = M.live || !M.seekable;
    if (M.seekable && !M.live && audio.duration) sk.value = (audio.currentTime / audio.duration) * 100;
  }
  function paintBtn() {
    if (!bar) return;
    $("#ny-play").textContent = audio.paused ? "▶" : "⏸";
    $("#ny-shuffle").classList.toggle("act", M.shuffle);
    $("#ny-shuffle").title = M.shuffle ? "Shuffle on" : "Shuffle off";
    const rep = $("#ny-repeat");
    rep.classList.toggle("act", M.repeat !== "off");
    rep.textContent = M.repeat === "one" ? "🔂" : "🔁";
    const repeatLabel = M.repeat === "one" ? "Song" : M.repeat === "all" ? "Playlist" : "Off";
    rep.title = "Loop: " + repeatLabel + " (click to change)";
    rep.setAttribute("aria-label", rep.title);
    rep.setAttribute("aria-pressed", M.repeat === "off" ? "false" : "true");
    const repeatState = $("#ny-repeat-state"); if (repeatState) repeatState.textContent = repeatLabel;
    if (repeatState) repeatState.classList.toggle("on", M.repeat !== "off");
    const current = M.queue[M.idx];
    const copy = $("#ny-copy");
    copy.disabled = !(current && current.url);
    copy.title = copy.disabled ? "No SoundCloud link for this track" : "Copy SoundCloud link";
    if (S.view === "music") renderTab();
  }

  // ---- playback -----------------------------------------------------------
  const trackKey = (t) => (t.kind === "sc" || t.url ? t.url : t.path);
  const srcFor = (t) => (t.kind === "sc" || t.url)
    ? "/api/music/sc-stream?url=" + encodeURIComponent(t.url)
    : "/api/music/track?path=" + encodeURIComponent(t.path);
  const titleOf = (t) => (t.kind === "sc" || t.url ? t.title : t.name) || "Untitled";
  const subOf = (t) => (t.kind === "sc" || t.url ? (t.artist || "SoundCloud") : (t.folder || "music"));

  function trackForApi(t) {
    if (!t) return null;
    return t.url ? { ...t, kind: "sc" } : { ...t, kind: "local" };
  }
  function historyBody(action) {
    const t = M.queue[M.idx]; if (!M.historyId || !t || M.live) return null;
    let seconds = M.historyElapsed;
    if (M.historySince && !audio.paused) seconds += (performance.now() - M.historySince) / 1000;
    return { action, id: M.historyId, seconds: Math.floor(seconds), track: trackForApi(t) };
  }
  function beginHistory(track) {
    if (!M.historyId) {
      M.historyId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
      M.historyElapsed = 0;
      postJSON("/api/music/history", { action: "start", id: M.historyId, seconds: 0, track: trackForApi(track) }).catch(() => {});
    }
    if (!M.historySince) M.historySince = performance.now();
  }
  function historyProgress(flush, keepalive) {
    const t = M.queue[M.idx];
    if (!M.historyId || !t || M.live) return;
    if (M.historySince) {
      M.historyElapsed += Math.max(0, (performance.now() - M.historySince) / 1000);
      M.historySince = audio.paused ? 0 : performance.now();
    }
    if (!flush && Date.now() - M.lastHistorySave < 15000) return;
    const body = historyBody("progress");
    if (body) {
      M.lastHistorySave = Date.now();
      if (keepalive) fetch("/api/music/history", { method: "POST", keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify(body) }).catch(() => {});
      else postJSON("/api/music/history", body).catch(() => {});
    }
  }
  function savePlayback(handoff, keepalive) {
    if (M.idx < 0 || !M.queue.length || M.live || !audio) return;
    const history = historyBody("progress");
    const body = { queue: M.queue.map(trackForApi), index: M.idx, position: audio.currentTime || 0,
      volume: audio.volume, playing: !audio.paused, shuffle: M.shuffle, repeat: M.repeat,
      history_id: M.historyId, history_seconds: history ? history.seconds : M.historyElapsed, handoff: !!handoff };
    if (!keepalive && Date.now() - M.lastPlaybackSave < 15000) return;
    M.lastPlaybackSave = Date.now();
    if (keepalive) {
      fetch("/api/music/playback", { method: "POST", keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify(body) }).catch(() => {});
    } else postJSON("/api/music/playback", body).catch(() => {});
  }
  async function restorePlayback() {
    if (M.restored) return;
    M.restored = true;
    try {
      const r = await getJSON("/api/music/playback");
      const state = r.playback || {};
      // The native app replaces the Music WKWebView when the user selects AI Galaxy, so pagehide is not guaranteed.
      // Restore any recent player state; the `playing` flag still keeps paused tracks paused.
      if (!r.ok || !Array.isArray(state.queue) || !state.queue.length || Date.now() / 1000 - (+state.updated || 0) > 180) return;
      ensureBar();
      M.queue = state.queue; M.idx = Math.max(0, Math.min(+state.index || 0, M.queue.length - 1));
      M.shuffle = !!state.shuffle; M.repeat = ["off", "all", "one"].includes(state.repeat) ? state.repeat : "off";
      M.historyId = state.history_id || ""; M.historyElapsed = Math.max(0, +state.history_seconds || 0); M.historySince = 0;
      const track = M.queue[M.idx]; if (!track) return;
      if (M.shuffle) resetShuffle(M.idx);
      audio.volume = Math.max(0, Math.min(1, +state.volume || 0));
      audio.src = srcFor(track);
      const resumeAt = Math.max(0, +state.position || 0);
      audio.addEventListener("loadedmetadata", () => { if (resumeAt < audio.duration) audio.currentTime = resumeAt; }, { once: true });
      $("#ny-np-title").textContent = titleOf(track); $("#ny-np-sub").textContent = subOf(track) + " · resuming…";
      $("#ny-vol").value = audio.volume; bar.classList.add("on"); paintBtn();
      if (state.playing) audio.play().catch(() => { $("#ny-np-sub").textContent = subOf(track) + " · press play to resume"; });
      savePlayback(false, true);
    } catch (e) { /* music state is optional */ }
  }
  window.addEventListener("pagehide", () => {
    historyProgress(true, true);
    savePlayback(true, true);
  });

  function playQueue(tracks, i) {
    historyProgress(true);
    M.historyId = ""; M.historyElapsed = 0; M.historySince = 0;
    M.queue = (tracks || []).slice();
    play(i || 0);
  }
  function resetShuffle(exclude) {
    M.shuffleOrder = null; M.shufflePos = 0;
    if (!M.shuffle || M.queue.length < 2) return;
    const order = Array.from({ length: M.queue.length }, (_, i) => i).filter((i) => i !== exclude);
    for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
    M.shuffleOrder = order;
  }
  function play(i, shuffled) {
    ensureBar();
    if (i < 0 || i >= M.queue.length) return;
    M.stopping = false;
    historyProgress(true);
    M.historyId = ""; M.historyElapsed = 0; M.historySince = 0; M.lastHistorySave = 0;
    M.idx = i; M.live = false;
    if (M.shuffle && !shuffled) resetShuffle(i);
    const t = M.queue[i];
    audio.defaultPlaybackRate = 1;
    audio.playbackRate = 1;
    const sc = (t.kind === "sc" || t.url);
    M.seekable = true;     // the server buffers both local files and SoundCloud, so seeking & resume work
    audio.src = srcFor(t);
    audio.play().then(() => savePlayback(false, true)).catch(() => {});
    $("#ny-np-title").textContent = titleOf(t);
    $("#ny-np-sub").textContent = sc ? subOf(t) + " · loading…" : subOf(t);
    $("#ny-live-pill").hidden = true;
    bar.classList.add("on");
    paintBtn();
  }
  async function onAudioError() {
    if (M.live) return;
    const t = M.queue[M.idx]; if (!t) return;
    if (t.kind === "sc" || t.url) {      // get the server's reason (DRM-protected, private, …)
      try {
        const r = await fetch(srcFor(t), { headers: { Range: "bytes=0-1" } });
        if (!r.ok) { toast((await r.text()) || "Couldn't play that track."); return; }
      } catch (e) { /* fall through */ }
    }
    toast("Couldn't play that track.");
  }
  function onEnded() {
    if (M.live) return;
    historyProgress(true);
    const body = historyBody("complete"); if (body) postJSON("/api/music/history", body).catch(() => {});
    if (M.repeat === "one") { play(M.idx); return; }
    next();
  }
  function next() {
    if (M.shuffle && M.queue.length > 1) {
      if (!M.shuffleOrder) resetShuffle(M.idx);
      if (M.shufflePos >= M.shuffleOrder.length) {
        if (M.repeat !== "all") { audio.pause(); return; }
        resetShuffle(M.idx);
      }
      play(M.shuffleOrder[M.shufflePos++], true);
      return;
    }
    if (M.idx + 1 < M.queue.length) play(M.idx + 1);
    else if (M.repeat === "all" && M.queue.length) play(0);
    else audio.pause();
  }
  function stop() {
    historyProgress(true);
    M.stopping = true;
    if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
    if (bar) bar.classList.remove("on");
    M.live = false; M.idx = -1; M.historyId = "";
    postJSON("/api/music/playback", { clear: true }).catch(() => {});
    if (S.view === "music") renderTab();
  }
  function playLive() {
    ensureBar();
    historyProgress(true); M.stopping = true;
    M.live = true; M.idx = -1; M.seekable = false;
    audio.src = "/api/audio/live?t=" + Date.now();
    audio.play().catch(() => toast("Couldn't start the live audio."));
    postJSON("/api/music/playback", { clear: true }).catch(() => {});
    $("#ny-np-title").textContent = "Server audio";
    $("#ny-np-sub").textContent = "live from the host";
    $("#ny-live-pill").hidden = false;
    bar.classList.add("on");
    paintBtn();
  }

  // ---- track rows ---------------------------------------------------------
  const isPlaying = (t) => M.idx >= 0 && !M.live && M.queue[M.idx] && trackKey(M.queue[M.idx]) === trackKey(t) && audio && !audio.paused;

  function trackRow(t, onplay, extra) {
    const dur = t.duration_ms ? fmtTime(t.duration_ms / 1000) : (t.size ? (t.size / 1048576).toFixed(1) + " MB" : "");
    const playing = isPlaying(t);
    return html`<div class="sc-row">
      <button class="sc-pbtn ${playing ? "" : "ghost"}" data-play="${onplay}" title="Play">${playing ? "⏸" : "▶"}</button>
      <div class="meta"><div><b>${titleOf(t)}</b></div><div class="faint small">${subOf(t)}${dur ? " · " + dur : ""}</div></div>
      ${extra || ""}
      <button class="btn small" data-music="add" data-key="${trackKey(t)}" title="Add to a playlist">＋</button>
    </div>`;
  }

  // ---- the local AI DJ ----------------------------------------------------
  function saveDJ() {
    try {
      localStorage.setItem("ny-music-dj", JSON.stringify({ active: !!M.dj.active, target: M.dj.target || "", voice: M.dj.voice || "",
        speak: M.dj.speak !== false, messages: (M.dj.messages || []).slice(-16), draft: M.dj.draft || null }));
    } catch (e) { /* private browser storage may be disabled */ }
  }
  const DJ_ROBOTIC = /\b(albert|bad news|bahh|bells|boing|bubbles|cellos|eddy|flo|fred|good news|grandma|grandpa|jester|junior|kathy|organ|ralph|reed|rocko|sandy|shelley|superstar|trinoids|whisper|wobble|zarvox|compact|eloquence)\b/i;
  function djVoices() { return typeof speechSynthesis === "undefined" ? [] : speechSynthesis.getVoices(); }
  function djVoiceScore(v) {
    const want = (navigator.language || "en").slice(0, 2).toLowerCase();
    let score = (v.lang || "").toLowerCase().startsWith(want) ? 100 : 0;
    if (/premium|neural|natural/i.test(v.name)) score += 60;
    else if (/enhanced|siri/i.test(v.name)) score += 45;
    else if (/^google/i.test(v.name)) score += 35;
    if (DJ_ROBOTIC.test(v.name)) score -= 90;
    if (v.default) score += 4;
    if (v.localService) score += 2;
    return score;
  }
  function djBestVoice() { return djVoices().slice().sort((a, b) => djVoiceScore(b) - djVoiceScore(a))[0] || null; }
  function djVoiceOptions() {
    const voices = djVoices().slice().sort((a, b) => djVoiceScore(b) - djVoiceScore(a)), best = djBestVoice();
    return '<option value=""' + (M.dj.voice ? "" : " selected") + ">Automatic" + (best ? ": " + esc(best.name) : " (browser default)") + "</option>" +
      voices.map((v) => '<option value="' + esc(v.name) + '"' + (v.name === M.dj.voice ? " selected" : "") + ">" + esc(v.name + " (" + v.lang + ")" + (djVoiceScore(v) >= 135 ? " · natural" : "")) + "</option>").join("");
  }
  function speakDJ(text) {
    if (M.dj.speak === false || typeof speechSynthesis === "undefined" || typeof SpeechSynthesisUtterance === "undefined") return;
    const clean = String(text || "").replace(/https?:\/\/\S+/g, "a link").replace(/[*_`#]/g, "").replace(/\s+/g, " ").trim();
    if (!clean) return;
    speechSynthesis.cancel();
    const voice = djVoices().find((v) => v.name === M.dj.voice) || djBestVoice();
    let rest = clean;
    while (rest) {
      let end = Math.min(rest.length, 220);
      if (end < rest.length) { const cut = rest.lastIndexOf(" ", end); if (cut > 100) end = cut; }
      const utterance = new SpeechSynthesisUtterance(rest.slice(0, end));
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
      utterance.rate = 0.96; utterance.pitch = 1.02;
      speechSynthesis.speak(utterance);
      rest = rest.slice(end).trimStart();
    }
  }
  function djTargets() {
    if (M.dj.targets === null) loadDJTargets();
    const ready = ((M.dj.targets || {}).targets || []).filter((x) => x.ready);
    const keep = M.dj.target;
    return ready.length
      ? ready.map((x) => '<option value="' + esc(x.id) + '"' + (x.id === keep ? " selected" : "") + ">" + esc(x.name + (x.kind === "ollama" ? " · " + (x.detail || "Ollama") : " · split model")) + "</option>").join("")
      : '<option value="">' + (M.dj.targets ? "No model is ready" : "Loading models…") + "</option>";
  }
  async function loadDJTargets() {
    if (M.dj.targets !== null || M.dj.loadingTargets) return;
    M.dj.loadingTargets = true;
    try {
      const r = await getJSON("/api/ai/targets");
      M.dj.targets = r.ok ? r : { targets: [], error: r.error || "Couldn't load the AI models." };
      const ready = (r.targets || []).filter((x) => x.ready);
      if (!ready.some((x) => x.id === M.dj.target)) M.dj.target = ready.length ? ready[0].id : "";
    } catch (e) { M.dj.targets = { targets: [], error: e.message || "Couldn't load the AI models." }; }
    M.dj.loadingTargets = false;
    if (S.view === "music" && M.tab === "dj") renderTab();
  }
  function renderDJ(el) {
    const ready = ((M.dj.targets || {}).targets || []).some((x) => x.ready);
    const voiceNote = typeof speechSynthesis === "undefined"
      ? "Speech isn't available in this browser; the DJ will still answer in text."
      : "Uses your browser's best available voice. Enhanced, Natural, Neural or Premium voices sound most lifelike when installed.";
    if (!M.dj.active) {
      setHTML(el, html`<div class="card">
        <div class="row" style="justify-content:space-between;align-items:center;gap:10px"><div><h3 style="margin:0">Nodeyard DJ</h3><div class="muted small mt">A private, local AI host for your music. Ask for a vibe, activity, or custom playlist.</div></div><span class="chip">Ready when you are</span></div>
        <p class="small mt">The DJ can use your music library, local likes and listening history. It only picks tracks already in that collection.</p>
        <div class="row wrap mt" style="gap:10px;align-items:flex-end">
          <label class="small">AI model<select class="select mt" data-music="dj-target" ${ready ? "" : "disabled"}>${raw(djTargets())}</select></label>
          ${typeof speechSynthesis !== "undefined" ? html`<label class="small">Voice<select class="select mt" data-music="dj-voice">${raw(djVoiceOptions())}</select></label>` : ""}
          <button class="btn primary" data-music="dj-start" ${ready ? "" : "disabled"}>▶ Start DJ</button>
        </div>
        <div class="faint small mt">${voiceNote}</div>
        ${M.dj.targets && !ready ? html`<p class="note warn small mt">${M.dj.targets.error || "No AI model is ready. Start one under AI → Models, then come back here."}</p>` : ""}
      </div>`);
      if (M.dj.targets === null) loadDJTargets();
      return;
    }
    const messages = (M.dj.messages || []).filter((m) => !m.hidden);
    setHTML(el, html`<div class="card">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h3 style="margin:0">Nodeyard DJ <span class="chip good">${M.dj.busy ? "thinking" : "on air"}</span></h3><div class="faint small mt">Ask for a mood, moment, or playlist. The DJ knows your likes and recent plays.</div></div>
        <button class="btn small" data-music="dj-stop">Stop DJ</button></div>
      <div class="row wrap mt" style="gap:8px;align-items:flex-end">
        <label class="small">AI model<select class="select mt" data-music="dj-target">${raw(djTargets())}</select></label>
        ${typeof speechSynthesis !== "undefined" ? html`<label class="small">Voice<select class="select mt" data-music="dj-voice">${raw(djVoiceOptions())}</select></label>
          <label class="check small"><input type="checkbox" data-music="dj-speak" ${M.dj.speak !== false ? raw("checked") : ""}> <span>Speak replies</span></label>` : ""}
      </div>
      <div id="dj-chat" style="max-height:380px;overflow:auto;display:flex;flex-direction:column;gap:10px;margin-top:14px">
        ${messages.map((m) => html`<div style="align-self:${m.role === "user" ? "flex-end" : "flex-start"};max-width:min(88%,720px);padding:10px 13px;border-radius:14px;background:${m.role === "user" ? "rgba(255,85,0,.16)" : "var(--panel2,rgba(125,160,200,.09))"}"><div class="small">${m.role === "assistant" ? "🎙 DJ" : "You"}</div><div style="white-space:pre-wrap;margin-top:3px">${m.content}</div></div>`)}
        ${M.dj.busy ? html`<div class="empty"><div class="spin"></div>DJ is thinking…</div>` : ""}
      </div>
      ${M.dj.draft && M.dj.draft.tracks && M.dj.draft.tracks.length ? html`<div class="mt" style="border-top:1px solid var(--line);padding-top:12px">
        <div class="row wrap" style="justify-content:space-between;align-items:center;gap:8px"><b>Mix preview: ${M.dj.draft.name} <span class="faint small">(${M.dj.draft.tracks.length} tracks)</span></b>
          <div class="row" style="gap:6px"><button class="btn small primary" data-music="dj-play">▶ Play mix</button><button class="btn small" data-music="dj-save" ${M.dj.draft.saved ? "disabled" : ""}>${M.dj.draft.saved ? "Saved ✓" : "Save playlist"}</button></div></div>
        <div class="mt">${M.dj.draft.tracks.map((t, i) => trackRow(t, "dj:" + i))}</div></div>` : ""}
      <div class="row wrap mt" style="gap:8px;align-items:flex-end">
        <textarea class="input" data-music="dj-request" rows="2" placeholder="What are you in the mood for? e.g. make me a mellow late-night mix from songs I've liked" style="flex:1;min-width:240px;resize:vertical" ${M.dj.busy ? "disabled" : ""}></textarea>
        <button class="btn primary" data-music="dj-send" ${M.dj.busy ? "disabled" : ""}>Ask DJ</button>
      </div>
      <div class="faint small mt">${voiceNote}</div>
    </div>`);
    const chat = $("#dj-chat"); if (chat) chat.scrollTop = chat.scrollHeight;
  }
  async function djCatalog() {
    const actions = ["liked", "history", "library"];
    const pages = await Promise.all(actions.map((action) => getJSON("/api/music/ai?action=" + action + "&offset=0&limit=50").catch(() => ({ ok: false, tracks: [] }))));
    const map = new Map();
    function add(track, source) {
      const key = track && (track.url || track.path); if (!key) return;
      let item = map.get(key);
      if (!item) { item = { track, sources: new Set(), plays: 0, lastPlayed: 0 }; map.set(key, item); }
      item.sources.add(source);
      if (source === "history") { item.plays++; item.lastPlayed = Math.max(item.lastPlayed, +track.played_at || 0); }
    }
    pages.forEach((page, i) => (page.tracks || []).forEach((track) => add(track, actions[i])));
    const rows = Array.from(map.values()).slice(0, 150);
    return rows.map((item, i) => {
      const t = item.track, local = !t.url;
      return { id: "t" + String(i + 1).padStart(3, "0"), track: { ...t, kind: local ? "local" : "sc" },
        info: { id: "t" + String(i + 1).padStart(3, "0"), title: local ? t.name : t.title, artist: local ? (t.folder || "your library") : (t.artist || "SoundCloud"),
          sources: Array.from(item.sources), plays: item.plays, last_played: item.lastPlayed || undefined } };
    });
  }
  async function readDJStream(response) {
    if (!response.ok) { let body = {}; try { body = await response.json(); } catch (e) { } throw new Error(body.error || "The AI model couldn't answer."); }
    if (!response.body || !response.body.getReader) return (await response.text());
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let pending = "", answer = "";
    while (true) {
      const part = await reader.read();
      pending += decoder.decode(part.value || new Uint8Array(), { stream: !part.done });
      const lines = pending.split(/\r?\n/); pending = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim(); if (!data || data === "[DONE]") continue;
        let event; try { event = JSON.parse(data); } catch (e) { continue; }
        if (event.error) throw new Error(typeof event.error === "string" ? event.error : (event.error.message || "The model stopped."));
        const delta = event.choices && event.choices[0] && event.choices[0].delta;
        if (delta && typeof delta.content === "string") answer += delta.content;
        else if (delta && Array.isArray(delta.content)) answer += delta.content.map((x) => x.text || "").join("");
      }
      if (part.done) break;
    }
    return answer;
  }
  function parseDJAnswer(text) {
    const clean = String(text || "").replace(/^\s*```(?:json)?\s*/i, "").replace(/\s*```\s*$/, "");
    const first = clean.indexOf("{"), last = clean.lastIndexOf("}");
    if (first < 0 || last <= first) return { speech: clean, playlist_name: "", track_ids: [] };
    try {
      const obj = JSON.parse(clean.slice(first, last + 1));
      return obj && typeof obj === "object" ? obj : { speech: clean, playlist_name: "", track_ids: [] };
    } catch (e) { return { speech: clean, playlist_name: "", track_ids: [] }; }
  }
  async function sendDJRequest(prompt, hidden) {
    if (M.dj.busy) return;
    if (!M.dj.target) { toast("Choose a ready AI model first."); return; }
    const userText = String(prompt || "").trim().slice(0, 4000); if (!userText) return;
    if (!hidden) M.dj.messages.push({ role: "user", content: userText });
    M.dj.busy = true; M.dj.error = "";
    const controller = new AbortController(); M.dj.controller = controller; saveDJ(); renderTab();
    try {
      const catalog = await djCatalog();
      if (!catalog.length) throw new Error("There are no songs for the DJ yet. Set a Music folder, like some SoundCloud songs, or listen to a few tracks first.");
      const catalogText = catalog.map((x) => x.info);
      const system = "You are Nodeyard DJ, a warm, confident, conversational music host. Sound like a natural radio presenter: short, personal, lightly playful, never overdo catchphrases. Use the listening history and likes to personalize, but never claim you listened to audio or know lyrics unless given. The user owns or selected this collection; recommend only tracks listed in the catalogue below. The user request may be a vibe, activity, artist/style preference, or a follow-up. If you need one key detail, ask one concise question and return no tracks. Otherwise create a useful mix. Reply with exactly one JSON object and no markdown: {\"speech\":\"spoken response, 1-3 short sentences\",\"playlist_name\":\"short name or empty string\",\"track_ids\":[\"t001\"]}. For a playlist, include 5-20 catalogue IDs in a deliberate order. If there are fewer suitable songs, use fewer. If this is only a greeting or clarifying question, playlist_name must be empty and track_ids must be []. Never invent IDs.\nCATALOGUE:\n" + JSON.stringify(catalogText);
      const history = (M.dj.messages || []).slice(-8).filter((m) => !m.hidden);
      if (!hidden && history.length && history[history.length - 1].role === "user" && history[history.length - 1].content === userText) history.pop();
      const messages = [{ role: "system", content: system }].concat(history.map((m) => ({ role: m.role, content: m.content })), [{ role: "user", content: userText }]);
      const response = await fetch("/api/v1/chat/completions", { method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, signal: controller.signal,
        body: JSON.stringify({ model: M.dj.target, messages, temperature: 0.65, max_tokens: 1400, stream: true }) });
      const parsed = parseDJAnswer(await readDJStream(response));
      const speech = String(parsed.speech || parsed.response || "").trim().slice(0, 2400) || "I couldn't put that into words. Try asking me a different way.";
      const byId = new Map(catalog.map((x) => [x.id, x.track]));
      const selected = Array.isArray(parsed.track_ids) ? parsed.track_ids.slice(0, 40).map((id) => byId.get(String(id))).filter(Boolean) : [];
      const name = String(parsed.playlist_name || "").trim().slice(0, 120);
      const assistantText = selected.length ? speech + "\n\nI’ve put together “" + (name || "Your mix") + "” with " + selected.length + " tracks." : speech;
      M.dj.messages.push({ role: "assistant", content: assistantText });
      if (selected.length && name) M.dj.draft = { name, tracks: selected, saved: false };
      else M.dj.draft = null;
      saveDJ(); speakDJ(speech);
    } catch (e) {
      const message = e.name === "AbortError" ? "I stopped that request." : (e.message || "The DJ couldn't answer.");
      if (e.name !== "AbortError") { M.dj.messages.push({ role: "assistant", content: message }); M.dj.error = message; }
      saveDJ();
    } finally {
      if (M.dj.controller === controller) { M.dj.busy = false; M.dj.controller = null; saveDJ(); renderTab(); }
    }
  }
  function startDJ() {
    if (!M.dj.target) { toast("No ready AI model. Start one under AI → Models."); return; }
    M.dj.active = true; M.dj.messages = []; M.dj.draft = null; M.dj.speak = M.dj.speak !== false; saveDJ(); renderTab();
    sendDJRequest("Start my DJ session. Welcome me in one short, warm line, then ask what vibe or activity I want. Do not make a playlist yet.", true);
  }
  function stopDJ() {
    if (M.dj.controller) M.dj.controller.abort();
    M.dj.controller = null;
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    M.dj.active = false; M.dj.busy = false; saveDJ(); renderTab();
  }

  // ---- tabs ---------------------------------------------------------------
  function renderPage() {
    const el = $("#view"); if (!el || S.view !== "music") return;
    const d = M.data || {};
    M.scLikes = Array.isArray((d.sc || {}).likes) ? d.sc.likes : [];
    M.playlists = Array.isArray(d.playlists) ? d.playlists : [];
    const tabs = [["search", "Search"], ["dj", "DJ"], ["playlists", "Playlists"], ["likes", "Likes"], ["library", "Library"], ["settings", "Settings"]];
    NY.freshHTML(el, html`
      <div class="row" style="align-items:center;gap:10px;margin-bottom:6px">
        <div style="font-size:22px;font-weight:800;letter-spacing:.3px">nodecloud <span style="color:var(--sc,#ff5500)">♪</span></div>
        <div class="faint small">your own SoundCloud, served by the cluster</div>
      </div>
      <canvas id="sc-viz-top" class="ny-viz-top" width="1000" height="56" aria-hidden="true"></canvas>
      <div class="sc-tabs">${raw(tabs.map(([k, n]) => '<button data-tab="' + k + '" class="' + (M.tab === k ? "on" : "") + '">' + n + "</button>").join(""))}</div>
      <div id="sc-tab"></div>`);
    renderTab();
    if (audio && !audio.paused) startViz();     // paint the top-bar visualizer if something's already playing
  }

  function renderTab() {
    if (S.view !== "music") return;
    const el = $("#sc-tab"); if (!el || !M.data) return;
    if (M.tab === "search") renderSearch(el);
    else if (M.tab === "dj") renderDJ(el);
    else if (M.tab === "playlists") renderPlaylists(el);
    else if (M.tab === "likes") renderLikes(el);
    else if (M.tab === "library") renderLibrary(el);
    else if (M.tab === "settings") renderSettings(el);
  }

  function renderSearch(el) {
    const sc = (M.data || {}).sc || {};
    const setBody = M.scSet
      ? html`<div class="row" style="justify-content:space-between;align-items:center;margin-top:4px">
          <b>${M.scSet.title} <span class="faint small">(${M.scSet.tracks.length})</span></b>
          <button class="btn small primary" data-music="play-set">▶ Play all</button></div>
        ${M.scSet.tracks.map((t) => trackRow(t, "set:" + trackKey(t), likeBtn(t)))}`
      : "";
    const results = M.scResults.length
      ? html`<div class="section-title">Results</div>${M.scResults.map((t) => trackRow(t, "res:" + trackKey(t), likeBtn(t)))}`
      : (M._searched ? html`<div class="empty">No ${M.hideDrm ? "playable " : ""}results${M.hideDrm ? " — all matches were DRM-protected" : ""}.</div>` : "");
    setHTML(el, html`
      <div class="card">
        <div class="row" style="gap:8px"><input class="input" data-music="sc-query" placeholder="Search SoundCloud for a song, artist…" spellcheck="false" style="flex:1">
          <button class="btn primary" data-music="sc-search" ${sc.search_ready ? "" : "disabled"}>Search</button></div>
        <label class="check mt" style="font-size:13px"><input type="checkbox" data-music="nodrm" ${M.hideDrm ? raw("checked") : ""}> <span>Only show playable tracks — hide DRM-protected (Go+) ones <span class="faint">(slower; checks each result)</span></span></label>
        ${!sc.search_ready ? html`<p class="small mt">SoundCloud search needs <span class="mono">yt-dlp</span> on the server. Use <b>Settings → Update everything</b> (installs it), then search.</p>` : ""}
        ${sc.search_ready && !sc.stream_ready ? html`<p class="small faint mt">In-browser playback also needs ffmpeg — run <b>Settings → Update everything</b>. Until then use ＋ to save tracks and ↗ to open on SoundCloud.</p>` : ""}
        <div id="sc-results" class="mt">${results}</div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Play a song link</h3>
        <p class="muted small" style="margin-top:0">Paste a link to one SoundCloud song to open it here.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="track-url" value="${M._trackUrl || ""}" placeholder="https://soundcloud.com/artist/song" spellcheck="false" style="flex:1">
          <button class="btn primary" data-music="play-url">Play link</button></div>
        <div id="sc-track-link-status" class="mt"></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Open a SoundCloud playlist / set</h3>
        <p class="muted small" style="margin-top:0">Paste a playlist, set or artist URL to load its tracks — play them or ＋ save them to a local playlist.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="set-url" placeholder="https://soundcloud.com/…/sets/…" spellcheck="false" style="flex:1">
          <button class="btn primary" data-music="load-set">Load</button></div>
        <div id="sc-set" class="mt">${setBody}</div>
      </div>`);
  }
  function likeBtn(t) {
    const liked = M.scLikes.some((x) => x.url === t.url);
    return html`<button class="btn small" data-music="sc-like" data-key="${t.url}" title="${liked ? "Unlike" : "Like"}">${liked ? "♥" : "♡"}</button>
      <button class="btn small" data-music="sc-dl" data-key="${t.url}" title="Download to the music folder">⬇</button>`;
  }

  function renderPlaylists(el) {
    if (M.openPl) {
      const p = M.openPl;
      setHTML(el, html`<div class="card">
        <div class="row" style="justify-content:space-between;align-items:center">
          <div class="row" style="gap:8px;align-items:center"><button class="btn small" data-music="pl-back">← Playlists</button>
            <b style="font-size:17px">${p.name}</b> <span class="faint small">(${(p.tracks || []).length})</span></div>
          <div class="row" style="gap:6px">
            ${(p.tracks || []).length ? raw('<button class="btn small primary" data-music="pl-play">▶ Play all</button>') : ""}
            <button class="btn small" data-music="pl-rename" data-id="${p.id}">Rename</button>
            <button class="btn small" data-music="pl-delete" data-id="${p.id}">Delete</button></div>
        </div>
        <div class="mt">${(p.tracks || []).length
          ? p.tracks.map((t, i) => trackRow(t, "pl:" + i, raw('<button class="btn small" data-music="pl-remove" data-i="' + i + '" title="Remove">✕</button>')))
          : html`<div class="empty">Empty. Add tracks with ＋ from Search, Likes or Library.</div>`}</div></div>`);
      return;
    }
    setHTML(el, html`<div class="card">
      <div class="row" style="gap:8px"><input class="input" data-music="pl-new" placeholder="New playlist name" style="flex:1">
        <button class="btn primary" data-music="pl-create">Create</button></div>
      <div class="mt">${M.playlists.length
        ? M.playlists.map((p) => html`<div class="sc-row">
            <button class="sc-pbtn ghost" data-music="pl-open" data-id="${p.id}" title="Open">♪</button>
            <div class="meta" data-music="pl-open" data-id="${p.id}" style="cursor:pointer"><div><b>${p.name}</b></div><div class="faint small">${p.count} track${p.count === 1 ? "" : "s"}</div></div>
            <button class="btn small" data-music="pl-delete" data-id="${p.id}">✕</button></div>`)
        : html`<div class="empty">No playlists yet. Create one above.</div>`}</div></div>`);
  }

  function renderLikes(el) {
    setHTML(el, html`<div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <b style="font-size:17px">♥ Likes <span class="faint small">(${M.scLikes.length})</span></b>
        ${M.scLikes.length ? raw('<button class="btn small primary" data-music="likes-play">▶ Play all</button>') : ""}</div>
      <p class="muted small">Saved in this dashboard — they don't touch your SoundCloud account.</p>
      <div class="mt">${M.scLikes.length
        ? M.scLikes.map((t, i) => trackRow(t, "like:" + i, likeBtn(t)))
        : html`<div class="empty">No likes yet. ♡ a track in Search.</div>`}</div></div>`);
  }

  function renderLibrary(el) {
    const tracks = (M.data || {}).tracks || [];
    const q = (M._libq || "").toLowerCase();
    const shown = q ? tracks.filter((t) => (t.name + " " + (t.folder || "")).toLowerCase().includes(q)) : tracks;
    setHTML(el, html`<div class="card">
      <div class="row" style="justify-content:space-between;align-items:center;gap:8px">
        <input class="input" data-music="lib-filter" value="${M._libq || ""}" placeholder="Filter the library…" style="flex:1">
        ${shown.length ? raw('<button class="btn small primary" data-music="lib-play">▶ Play all</button>') : ""}</div>
      ${!(M.data || {}).dir ? html`<p class="small mt">No music folder set — pick one in <b>Settings</b>.</p>` : ""}
      <div class="mt">${shown.length
        ? shown.map((t) => trackRow(t, "lib:" + trackKey(t)))
        : html`<div class="empty">${(M.data || {}).dir ? "No matching files." : "Set a music folder in Settings to see your library."}</div>`}</div></div>`);
  }

  function renderSettings(el) {
    const d = M.data || {}; const fw = d.forward || {};
    const srcOpts = ['<option value="">Automatic (the server\'s output)</option>']
      .concat((fw.sources || []).map((s) => '<option value="' + esc(s) + '"' + (s === fw.source ? " selected" : "") + ">" + esc(s) + "</option>")).join("");
    const fwBody = !fw.ffmpeg
      ? html`<p class="muted small">ffmpeg isn't installed on the server, so live audio forwarding isn't available.${fw.reason ? " " + fw.reason : ""}</p>`
      : html`<label class="check"><input type="checkbox" data-music="fw-toggle" ${fw.enabled ? raw("checked") : ""}> <span>Forward the server's audio output to the browser — captured <b>automatically</b></span></label>
        <div class="small faint mt">Capturing: <span class="mono">${fw.auto_source || "auto"}</span>${fw.source ? " (chosen)" : " (auto)"}</div>
        ${fw.reason ? html`<p class="err small mt">${fw.reason}</p>` : ""}
        <div class="row mt" style="gap:8px;align-items:center">
          <button class="btn small" data-music="fw-test">Test capture</button>
          ${fw.enabled && fw.available ? raw('<button class="btn small primary" data-music="listen">Listen now</button>') : ""}</div>
        ${!fw.available ? raw('<div class="mt"><button class="btn small primary" data-music="audio-setup">Set up server audio automatically</button> <span id="audio-setup-msg" class="small faint"></span><div class="small faint mt">Installs PulseAudio + a null sink on the server so forwarding has something to capture. Play audio on the server (e.g. sc-pick), then it forwards to any browser. Best-effort; redeploy afterwards.</div></div>') : ""}
        <details class="mt"><summary class="small faint">Choose a source instead of automatic</summary>
          ${raw('<select class="select mt" data-music="fw-source">' + srcOpts + "</select>")}</details>
        <div id="fw-test-out" class="small mt"></div>`;
    setHTML(el, html`
      ${card("Music folder", html`<p class="muted small" style="margin-top:0">The server plays audio files from one folder on the host (the control node, not your Mac). Everyone signed in can listen.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="dir" value="${d.dir || ""}" placeholder="/home/your-user/Music/soundcloud" spellcheck="false" style="flex:1">
          <button class="btn" data-music="browse">Browse…</button><button class="btn primary" data-music="save-dir">Save</button></div>`)}
      ${card("Live audio forwarding", fwBody)}`);
  }

  // ---- add-to-playlist picker --------------------------------------------
  function findTrack(key) {
    return [M.queue, M.scResults, M.scLikes, (M.scSet && M.scSet.tracks) || [], (M.openPl && M.openPl.tracks) || [],
      (M.dj.draft && M.dj.draft.tracks) || [], (M.data || {}).tracks || []]
      .flat().find((t) => t && trackKey(t) === key) || null;
  }
  function openAddPicker(track) {
    M.addTrack = track;
    let m = document.getElementById("pl-picker");
    if (!m) { m = document.createElement("div"); m.id = "pl-picker"; document.body.appendChild(m); }
    m.style = "position:fixed;inset:0;z-index:71;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5)";
    m.innerHTML =
      '<div class="card" style="width:min(440px,92vw);max-height:76vh;display:flex;flex-direction:column">' +
      '<h3>Add to playlist<span class="right"><button class="btn small" data-music="pk-cancel">Close</button></span></h3>' +
      '<div class="faint small" style="margin-bottom:8px">' + esc(titleOf(track)) + "</div>" +
      '<div class="row" style="gap:6px;margin-bottom:8px"><input class="input" id="pk-new" placeholder="New playlist name" style="flex:1"><button class="btn primary" data-music="pk-new">Create & add</button></div>' +
      '<div style="overflow:auto;flex:1">' +
      (M.playlists.length ? M.playlists.map((p) => '<button class="btn small" data-music="pk-add" data-id="' + esc(p.id) + '" style="display:block;width:100%;text-align:left;margin:3px 0">♪ ' + esc(p.name) + ' <span class="faint small">(' + p.count + ")</span></button>").join("") : '<div class="faint small">No playlists yet — make one above.</div>') +
      "</div></div>";
  }
  const closeAddPicker = () => { const m = document.getElementById("pl-picker"); if (m) m.remove(); M.addTrack = null; };
  async function addToPlaylist(pid, track) {
    const r = await postJSON("/api/music/playlist", { action: "add", id: pid, track });
    if (r.ok) { M.playlists = r.playlists || M.playlists; toast("Added."); } else toast(r.error || "Couldn't add.");
  }

  // ---- folder picker ------------------------------------------------------
  async function openBrowse(start) {
    const r = await getJSON("/api/music/browse" + (start ? "?path=" + encodeURIComponent(start) : ""));
    if (!r.ok) { toast(r.error || "Can't open that folder."); return; }
    M.browse = r; renderBrowse();
  }
  function renderBrowse() {
    let m = document.getElementById("music-browse");
    if (!m) { m = document.createElement("div"); m.id = "music-browse"; document.body.appendChild(m); }
    const b = M.browse || {};
    m.style = "position:fixed;inset:0;z-index:70;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5)";
    const shortcuts = (b.shortcuts || []).map((p) => '<button class="btn small" data-music="b-short" data-path="' + esc(p) + '">' + esc(p) + "</button>").join(" ");
    const dirs = (b.dirs || []).length
      ? (b.dirs || []).map((name) => '<button class="btn small" data-music="b-open" data-path="' + esc((b.path === "/" ? "" : b.path) + "/" + name) + '" style="display:block;width:100%;text-align:left;margin:2px 0">📁 ' + esc(name) + "</button>").join("")
      : '<div class="faint small" style="padding:8px">No sub-folders here.</div>';
    m.innerHTML =
      '<div class="card" style="width:min(560px,92vw);max-height:80vh;display:flex;flex-direction:column">' +
      '<h3>Pick a music folder<span class="right"><button class="btn small" data-music="b-cancel">Close</button></span></h3>' +
      '<div class="mono small faint" style="word-break:break-all;margin-bottom:6px">' + esc(b.path || "/") + (b.audio_here ? ' · <b>' + b.audio_here + " audio files here</b>" : " · no audio files here") + "</div>" +
      (shortcuts ? '<div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:6px">' + shortcuts + "</div>" : "") +
      '<div class="row" style="gap:6px;margin-bottom:6px">' + (b.parent ? '<button class="btn small" data-music="b-open" data-path="' + esc(b.parent) + '">⬆ Up</button>' : "") +
      '<button class="btn small primary" data-music="b-use" data-path="' + esc(b.path || "") + '">Use this folder</button></div>' +
      '<div style="overflow:auto;flex:1;border:1px solid var(--line);border-radius:8px;padding:6px">' + dirs + "</div></div>";
  }
  const closeBrowse = () => { const m = document.getElementById("music-browse"); if (m) m.remove(); };
  async function saveDir(dir) {
    const r = await postJSON("/api/music/config", { dir }); if (r.ok) { await load(); renderPage(); toast("Saved."); } else toast(r.error || "Not saved.");
  }

  async function load() { try { const r = await getJSON("/api/music"); if (r.ok) M.data = r; } catch (e) { /* signed out */ } }

  async function searchSc() {
    const input = $('[data-music="sc-query"]');
    const query = (input && input.value || "").trim();
    if (query.length < 2) { toast("Enter at least two characters to search."); return; }
    const el = $("#sc-results"); if (el) setHTML(el, html`<div class="empty"><div class="spin"></div>${M.hideDrm ? "Searching and checking which tracks are playable…" : "Searching…"}</div>`);
    try {
      const r = await getJSON("/api/music/search?q=" + encodeURIComponent(query) + (M.hideDrm ? "&nodrm=1" : ""));
      M._searched = true;
      if (!r.ok) { if (el) setHTML(el, html`<div class="empty">${r.error || "Search failed."}</div>`); return; }
      M.scResults = Array.isArray(r.tracks) ? r.tracks : [];
      renderTab();
    } catch (e) { if (el) setHTML(el, html`<div class="empty">${e.message || "Search failed."}</div>`); }
  }
  async function loadSet() {
    const input = $('[data-music="set-url"]');
    const url = (input && input.value || "").trim();
    if (!url) return;
    const el = $("#sc-set"); if (el) setHTML(el, html`<div class="empty"><div class="spin"></div>Loading playlist…</div>`);
    const r = await getJSON("/api/music/sc-playlist?url=" + encodeURIComponent(url));
    if (!r.ok) { if (el) setHTML(el, html`<div class="empty">${r.error || "Couldn't load that."}</div>`); return; }
    M.scSet = { title: r.title || "Playlist", tracks: r.tracks || [] };
    renderTab();
  }
  async function playTrackLink() {
    const input = $('[data-music="track-url"]');
    const url = (input && input.value || "").trim();
    if (!url) { toast("Paste a SoundCloud song link first."); return; }
    M._trackUrl = url;
    const out = $("#sc-track-link-status");
    if (out) setHTML(out, html`<div class="empty"><div class="spin"></div>Opening song link…</div>`);
    try {
      const r = await getJSON("/api/music/sc-playlist?url=" + encodeURIComponent(url));
      if (!r.ok) {
        if (out) setHTML(out, html`<div class="err small">${r.error || "Couldn't open that link."}</div>`);
        return;
      }
      const tracks = Array.isArray(r.tracks) ? r.tracks : [];
      if (tracks.length !== 1) {
        if (out) setHTML(out, html`<div class="err small">That link has multiple tracks. Load it in the playlist section below.</div>`);
        return;
      }
      playQueue([{ ...tracks[0], kind: "sc" }], 0);
      toast("Opened song link.");
    } catch (e) {
      if (out) setHTML(out, html`<div class="err small">${e.message || "Couldn't open that link."}</div>`);
    }
  }
  async function copyCurrentLink() {
    const track = M.queue[M.idx];
    if (!track || !track.url) { toast("This track has no SoundCloud link."); return; }
    try {
      await navigator.clipboard.writeText(track.url);
    } catch (e) {
      const input = document.createElement("textarea");
      input.value = track.url;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) { toast("Couldn't copy the link."); return; }
    }
    toast("SoundCloud link copied.");
  }

  // ---- events -------------------------------------------------------------
  document.addEventListener("click", async (e) => {
    const pb = e.target.closest && e.target.closest("[data-play]");
    if (pb) { startPlay(pb.dataset.play); return; }
    const t = e.target.closest && e.target.closest("[data-music], [data-tab]"); if (!t) return;
    if (t.dataset.tab) { M.tab = t.dataset.tab; M.openPl = null; renderPage(); return; }
    const act = t.dataset.music;
    if (act === "listen") playLive();
    else if (act === "dj-start") startDJ();
    else if (act === "dj-stop") stopDJ();
    else if (act === "dj-send") {
      const input = $('[data-music="dj-request"]'), request = (input && input.value || "").trim();
      if (input) input.value = "";
      if (request) sendDJRequest(request, false);
    }
    else if (act === "dj-play") { if (M.dj.draft && M.dj.draft.tracks.length) playQueue(M.dj.draft.tracks, 0); }
    else if (act === "dj-save") {
      if (!M.dj.draft || !M.dj.draft.tracks.length) return;
      const r = await postJSON("/api/music/ai", { action: "create_playlist", name: M.dj.draft.name, tracks: M.dj.draft.tracks });
      if (!r.ok) { toast(r.error || "Couldn't save the playlist."); return; }
      M.dj.draft.saved = true; await load(); M.playlists = (M.data || {}).playlists || M.playlists; saveDJ(); renderTab(); toast("Saved to Playlists.");
    }
    else if (act === "save-dir") await saveDir($('[data-music="dir"]').value.trim());
    else if (act === "browse") openBrowse($('[data-music="dir"]').value.trim());
    else if (act === "b-open" || act === "b-short") openBrowse(t.dataset.path);
    else if (act === "b-cancel") closeBrowse();
    else if (act === "b-use") { closeBrowse(); await saveDir(t.dataset.path); }
    else if (act === "sc-search") await searchSc();
    else if (act === "play-url") await playTrackLink();
    else if (act === "load-set") await loadSet();
    else if (act === "play-set") { if (M.scSet) playQueue(M.scSet.tracks, 0); }
    else if (act === "likes-play") playQueue(M.scLikes, 0);
    else if (act === "lib-play") playQueue(filteredLib(), 0);
    else if (act === "sc-like") await toggleLike(t.dataset.key);
    else if (act === "sc-dl") {
      toast("Downloading to the music folder…");
      const r = await postJSON("/api/music/sc-download", { url: t.dataset.key });
      toast(r.ok ? "Saved to the music folder." : (r.error || "Download failed."));
    }
    else if (act === "add") { const tr = findTrack(t.dataset.key); if (tr) openAddPicker(tr); }
    else if (act === "pk-cancel") closeAddPicker();
    else if (act === "pk-add") { await addToPlaylist(t.dataset.id, M.addTrack); closeAddPicker(); renderTab(); }
    else if (act === "pk-new") {
      const name = ($("#pk-new").value || "").trim(); const r = await postJSON("/api/music/playlist", { action: "create", name });
      if (r.ok) { M.playlists = r.playlists || []; await addToPlaylist(r.id, M.addTrack); } closeAddPicker(); renderTab();
    }
    else if (act === "pl-create") {
      const name = ($('[data-music="pl-new"]').value || "").trim();
      const r = await postJSON("/api/music/playlist", { action: "create", name }); if (r.ok) { M.playlists = r.playlists || []; renderTab(); }
    }
    else if (act === "pl-open") await openPlaylist(t.dataset.id);
    else if (act === "pl-back") { M.openPl = null; renderTab(); }
    else if (act === "pl-play") { if (M.openPl) playQueue(M.openPl.tracks, 0); }
    else if (act === "pl-rename") {
      const name = prompt("Rename playlist", (M.openPl || {}).name || ""); if (name == null) return;
      const r = await postJSON("/api/music/playlist", { action: "rename", id: t.dataset.id, name });
      if (r.ok) { M.playlists = r.playlists || []; if (M.openPl) M.openPl.name = name; renderTab(); }
    }
    else if (act === "pl-delete") {
      if (!confirm("Delete this playlist?")) return;
      const r = await postJSON("/api/music/playlist", { action: "delete", id: t.dataset.id });
      if (r.ok) { M.playlists = r.playlists || []; M.openPl = null; renderTab(); }
    }
    else if (act === "pl-remove") {
      if (!M.openPl) return;
      const r = await postJSON("/api/music/playlist", { action: "remove", id: M.openPl.id, index: +t.dataset.i });
      if (r.ok) { M.playlists = r.playlists || []; await openPlaylist(M.openPl.id); }
    }
    else if (act === "fw-test") {
      const out = $("#fw-test-out"); if (out) out.textContent = "Testing…";
      const r = await postJSON("/api/audio/test", {}); if (out) out.innerHTML = (r.ok ? '<span style="color:var(--ok,#3ecf8e)">✓ </span>' : '<span class="err">✕ </span>') + esc(r.message || r.error || "");
    }
    else if (act === "audio-setup") {
      const out = $("#audio-setup-msg"); if (out) out.textContent = "Setting up… (installing PulseAudio)";
      const r = await postJSON("/api/audio/setup-server", {}); if (out) out.textContent = r.message || r.error || "";
    }
  });

  function filteredLib() {
    const tracks = (M.data || {}).tracks || []; const q = (M._libq || "").toLowerCase();
    return q ? tracks.filter((t) => (t.name + " " + (t.folder || "")).toLowerCase().includes(q)) : tracks;
  }
  function startPlay(tok) {
    const [kind, rest] = [tok.slice(0, tok.indexOf(":")), tok.slice(tok.indexOf(":") + 1)];
    if (kind === "res") { const i = M.scResults.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(M.scResults, i); }
    else if (kind === "set") { if (M.scSet) { const i = M.scSet.tracks.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(M.scSet.tracks, i); } }
    else if (kind === "like") playQueue(M.scLikes, +rest);
    else if (kind === "pl") { if (M.openPl) playQueue(M.openPl.tracks, +rest); }
    else if (kind === "dj") { if (M.dj.draft) playQueue(M.dj.draft.tracks, +rest); }
    else if (kind === "lib") { const lib = filteredLib(); const i = lib.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(lib, i); }
  }
  async function toggleLike(url) {
    const track = findTrack(url); if (!track) return;
    const liked = !M.scLikes.some((x) => x.url === url);
    const r = await postJSON("/api/music/like", { track, liked });
    if (!r.ok) { toast(r.error || "Couldn't save that like."); return; }
    M.scLikes = Array.isArray(r.likes) ? r.likes : [];
    if (M.data && M.data.sc) M.data.sc.likes = M.scLikes;   // keep it through a renderPage() (tab switch)
    renderTab();
  }
  async function openPlaylist(id) {
    const r = await getJSON("/api/music/playlists?id=" + encodeURIComponent(id));
    if (r.ok && r.playlist) { M.openPl = r.playlist; renderTab(); } else toast("Couldn't open that playlist.");
  }

  document.addEventListener("input", (e) => {
    if (e.target.matches('[data-music="lib-filter"]')) { M._libq = e.target.value; renderTab(); const inp = $('[data-music="lib-filter"]'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.matches('[data-music="dj-request"]')) { e.preventDefault(); $('[data-music="dj-send"]')?.click(); return; }
    if (e.key !== "Enter") return;
    if (e.target.matches('[data-music="sc-query"]')) { e.preventDefault(); searchSc(); }
    else if (e.target.matches('[data-music="track-url"]')) { e.preventDefault(); playTrackLink(); }
    else if (e.target.matches('[data-music="set-url"]')) { e.preventDefault(); loadSet(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    if (t.dataset.music === "dj-target") { M.dj.target = t.value; saveDJ(); }
    else if (t.dataset.music === "dj-voice") { M.dj.voice = t.value; saveDJ(); }
    else if (t.dataset.music === "dj-speak") { M.dj.speak = t.checked; saveDJ(); if (!t.checked && typeof speechSynthesis !== "undefined") speechSynthesis.cancel(); }
    else if (t.dataset.music === "fw-toggle") { const r = await postJSON("/api/music/config", { forward_enabled: t.checked }); if (r.ok) { await load(); renderTab(); } }
    else if (t.dataset.music === "fw-source") { await postJSON("/api/music/config", { forward_source: t.value }); await load(); renderTab(); }
    else if (t.dataset.music === "nodrm") { M.hideDrm = t.checked; try { localStorage.setItem("ny-hidedrm", t.checked ? "1" : "0"); } catch (e) { /* ignore */ } }
  });

  V.music = {
    build() { ensureBar(); NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(renderPage); restorePlayback(); },
    update() { },
  };
  if (typeof speechSynthesis !== "undefined") speechSynthesis.addEventListener("voiceschanged", () => {
    const select = $('[data-music="dj-voice"]'); if (select) select.innerHTML = djVoiceOptions();
  });
})();

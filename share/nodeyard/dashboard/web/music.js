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
    const initial = { active: false, target: "", voice: "", speak: true, between: true, messages: [], draft: null, targets: null, busy: false, piper: null };
    try {
      const saved = JSON.parse(localStorage.getItem("ny-music-dj") || "{}");
      if (!saved || typeof saved !== "object") return initial;
      return Object.assign(initial, saved, {
        messages: Array.isArray(saved.messages) ? saved.messages.slice(-16).filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
          .map((m) => Object.assign({}, m, { content: m.role === "assistant" ? readableDJMessage(m.content) : m.content })) : [],
        draft: saved.draft && Array.isArray(saved.draft.tracks) ? saved.draft : null,
        targets: null, busy: false, piper: null,
      });
    } catch (e) { return initial; }
  }

  const M = {
    data: null, tab: "home", homeHistory: [], homeHistoryReady: false, homeHistoryError: false, homeSessionHeard: new Set(), homeRecs: [], homeUnheard: [],
    queue: [], idx: -1, live: false, seekable: true, shuffle: false, shuffleOrder: null, shufflePos: 0, repeat: "off", rate: 1, eq: "flat",
    scResults: [], playlistResults: [], searchQuery: "", searchError: "", scSet: null, scLikes: [], playlists: [], openPl: null, addTrack: null, uploadMessage: "",
    discoverTracks: [], discoverIndex: 0, discoverSeeded: false, discoverMoreBusy: false, discoverMoreRetry: 0, discoverWantNext: false, discoverPointerY: null, discoverWheelAt: 0,
    disc: { tracks: [], updated: 0, building: false, error: "", seeds: [], loaded: false, polls: 0, timer: 0 },   // new songs found for you
    dj: readDJState(),
    sleep: null,   // the sleep timer: null, "end" (after this song) or a time in ms
    historyId: "", historyElapsed: 0, historySince: 0, lastHistorySave: 0, lastPlaybackSave: 0, restored: false, stopping: false,
    hideDrm: (() => { try { return localStorage.getItem("ny-hidedrm") === "1"; } catch (e) { return false; } })(),
  };
  const EQ_PRESETS = {
    flat: [0, 0, 0, 0, 0], bass: [6, 3, 0, 0, 0], treble: [0, 0, 0, 3, 6],
    rock: [4, 2, -1, 2, 4], electronic: [5, 1, -2, 3, 5], vocal: [-1, 1, 3, 2, -1], classical: [2, 0, -1, 2, 3],
  };
  const EQ_OPTIONS = '<option value="flat">Flat</option><option value="bass">Bass boost</option><option value="treble">Treble</option><option value="rock">Rock</option><option value="electronic">Electronic</option><option value="vocal">Vocal</option><option value="classical">Classical</option>';
  let audio = null, bar = null;
  let actx = null, analyser = null, vizData = null, vizSource = null, vizRAF = 0, eqNodes = [];

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
      "#ny-player select{background:rgba(125,160,200,.10);border:1px solid var(--line,rgba(125,160,200,.18));color:inherit;border-radius:6px;padding:4px;font-size:12px}" +
      "#ny-player button:disabled{opacity:.4;cursor:default}" +
      "#ny-player button.act{color:var(--sc,#ff5500)}" +
      "#ny-player button.act::after{content:'';position:absolute;left:50%;bottom:1px;transform:translateX(-50%);width:4px;height:4px;border-radius:50%;background:var(--sc,#ff5500)}" +
      "#ny-player #ny-shuffle,#ny-player #ny-repeat,#ny-player #ny-sleep{position:relative}" +
      "#ny-player .loop-state{font-size:10px;font-weight:700;letter-spacing:.03em;color:var(--ink3,#8fa3c0);min-width:46px}" +
      "#ny-player .loop-state.on{color:var(--sc,#ff5500)}" +
      "#ny-player .seek{flex:1;display:flex;align-items:center;gap:8px;min-width:110px}" +
      "#ny-player input[type=range]{flex:1;accent-color:var(--sc,#ff5500)}" +
      "#ny-player .t{font-variant-numeric:tabular-nums;color:var(--ink3,#8fa3c0);font-size:11px;min-width:38px}" +
      "#ny-player .live-pill{color:#ff5a6a;font-weight:600;font-size:11px;letter-spacing:1px}" +
      "@media (max-width:1100px){#ny-player{flex-wrap:wrap;row-gap:4px}#ny-player canvas,#ny-player #ny-repeat-state,#ny-player #ny-sleep-state{display:none}}" +
      ".sc-tabs{display:inline-flex;gap:3px;flex-wrap:wrap;margin:6px 0 18px;padding:4px;background:rgba(125,160,200,.08);border:1px solid var(--line);border-radius:999px}" +
      ".sc-tabs button{background:none;border:0;color:var(--ink2,#b9c6dd);cursor:pointer;padding:7px 18px;border-radius:999px;font-size:14px;font-weight:600;transition:background .15s,color .15s,box-shadow .15s}" +
      ".sc-tabs button:hover{background:rgba(125,160,200,.15);color:var(--ink,#eaf1ff)}" +
      ".sc-tabs button.on{background:var(--sc,#ff5500);color:#fff;box-shadow:0 3px 10px rgba(255,85,0,.38)}" +
      ".sc-row{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;padding:8px 2px;border-bottom:1px solid var(--line)}" +
      ".sc-cover{position:relative;width:46px;height:46px;flex:none;border-radius:8px;overflow:hidden;display:grid;place-items:center;background:linear-gradient(135deg,rgba(255,85,0,.3),rgba(125,160,200,.16));color:var(--ink2,#b9c6dd);font-size:21px}" +
      ".sc-cover img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}" +
      ".sc-discover-feed{width:min(100%,640px);margin:0 auto}.sc-discover-feed-head{display:flex;justify-content:space-between;align-items:center;margin:0 4px 10px;color:var(--ink2,#b9c6dd)}.sc-discover-feed-head h2{margin:0;color:var(--ink,#eaf1ff)}.sc-discover-feed-head span{font-size:12px}.sc-discover-feed-head b{font-size:12px}" +
      ".sc-discover-card{position:relative;isolation:isolate;height:min(72dvh,800px);min-height:440px;overflow:hidden;border:1px solid rgba(255,255,255,.12);border-radius:22px;background-color:#151b2e;background-position:center;background-size:cover;color:white;touch-action:pan-x;overscroll-behavior:contain;box-shadow:0 16px 45px rgba(0,0,0,.25)}" +
      ".sc-discover-card::before{content:'';position:absolute;inset:-14px;z-index:-1;background:inherit;background-position:center;background-size:cover;filter:blur(18px) brightness(.55);transform:scale(1.05)}.sc-discover-shade{position:absolute;inset:0;z-index:0;background:linear-gradient(180deg,rgba(7,10,20,.08),rgba(7,10,20,.1) 34%,rgba(7,10,20,.9) 100%)}" +
      ".sc-discover-cover{position:absolute;z-index:1;top:8%;left:50%;transform:translateX(-50%);width:min(58%,330px);aspect-ratio:1;display:grid;place-items:center;overflow:hidden;border-radius:18px;background:rgba(8,12,22,.48);font-size:54px;box-shadow:0 16px 42px rgba(0,0,0,.38)}.sc-discover-cover img{width:100%;height:100%;object-fit:cover}" +
      ".sc-discover-actions{position:absolute;z-index:2;right:16px;bottom:120px;display:grid;justify-items:center;gap:5px}.sc-discover-actions button,.sc-discover-nav button{width:44px;height:44px;border:1px solid rgba(255,255,255,.35);border-radius:50%;background:rgba(12,16,28,.52);backdrop-filter:blur(10px);color:white;font-size:20px;cursor:pointer}.sc-discover-actions button:hover,.sc-discover-nav button:hover{background:rgba(255,85,0,.84)}.sc-discover-actions small{margin:0 0 8px;font-size:11px;text-shadow:0 1px 4px #000}" +
      ".sc-discover-meta{position:absolute;z-index:2;left:clamp(18px,4vw,40px);right:82px;bottom:58px;text-shadow:0 1px 5px rgba(0,0,0,.6)}.sc-discover-meta h2{font-size:clamp(22px,4vw,34px);line-height:1.12;margin:8px 0;overflow-wrap:anywhere}.sc-discover-meta>div{font-size:14px;color:rgba(255,255,255,.82)}.sc-discover-nav{position:absolute;z-index:3;right:18px;bottom:24px;display:flex;gap:7px}.sc-discover-nav button{width:36px;height:36px;font-size:17px}.sc-discover-nav button:disabled{opacity:.38;cursor:default}.sc-discover-hint{position:absolute;z-index:2;left:18px;bottom:20px;font-size:11px;color:rgba(255,255,255,.72);text-shadow:0 1px 4px #000}" +
      ".sc-row .meta{flex:1;min-width:0;overflow:hidden}" +
      ".sc-row .btn,.sc-row a.btn{flex:none}" +
      ".sc-row .meta div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
      ".sc-pbtn{flex:none;width:38px;height:38px;border-radius:50%;background:var(--sc,#ff5500);color:#fff;border:0;cursor:pointer;font-size:15px}" +
      ".sc-pbtn.ghost{background:rgba(125,160,200,.16);color:var(--ink,#eaf1ff)}" +
      ".ny-viz-top{display:block;width:100%;height:56px;margin:2px 0 14px;border-radius:12px;background:linear-gradient(180deg,rgba(255,85,0,.05),rgba(125,160,200,.06))}" +
      "@media (max-width:760px){#ny-player{bottom:56px;gap:3px;padding-left:10px;padding-right:10px}body.desk-mode #ny-player{bottom:0}#ny-player .np{max-width:88px}#ny-player canvas,#ny-player label,#ny-player #ny-copy,#ny-player #ny-repeat-state,#ny-player #ny-sleep-state,#ny-player #ny-sleep,#ny-player #ny-vol,#ny-player #ny-vol-i,#ny-player .t{display:none}#ny-player .seek{min-width:40px;overflow:hidden}#ny-player .seek input{min-width:0;width:100%}.sc-discover-card{height:min(70dvh,720px);min-height:400px}.sc-discover-cover{top:10%;width:min(68%,300px)}.sc-discover-meta{left:18px;bottom:62px}.sc-discover-actions{right:12px;bottom:118px}}";
    document.head.appendChild(style);
    document.addEventListener("error", (e) => { if (e.target && e.target.matches && e.target.matches(".sc-cover img")) e.target.remove(); }, true);
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
      '<label class="faint small" title="Playback speed">Speed <select id="ny-rate" aria-label="Playback speed"><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="1.75">1.75×</option><option value="2">2×</option></select></label>' +
      '<label class="faint small" title="Equalizer">EQ <select id="ny-eq" aria-label="Equalizer mode">' + EQ_OPTIONS + '</select></label>' +
      '<button id="ny-sleep" title="Sleep timer: off" aria-label="Sleep timer: off">⏾</button><span id="ny-sleep-state" class="loop-state"></span>' +
      '<button id="ny-vol-i" title="Mute" aria-label="Mute">🔊</button><input type="range" id="ny-vol" min="0" max="1" value="1" step="0.05" style="width:70px" aria-label="Volume">' +
      '<button id="ny-close" title="Close player">✕</button>';
    document.body.appendChild(bar);
    audio = new Audio();
    audio.preload = "metadata";
    audio.crossOrigin = "anonymous";
    audio.defaultPlaybackRate = 1;
    audio.playbackRate = 1;
    audio.preservesPitch = true;
    audio.webkitPreservesPitch = true;
    audio.addEventListener("ratechange", () => {
      const rate = Math.max(0.5, Math.min(2, +audio.playbackRate || 1));
      if (Math.abs(rate - M.rate) > 0.001) { M.rate = rate; $("#ny-rate").value = String(rate); savePlayback(false, true); }
    });
    audio.addEventListener("timeupdate", () => { tick(); historyProgress(false); savePlayback(false, false); prefetchNext(); });
    audio.addEventListener("durationchange", tick);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("play", () => { startViz(); paintBtn(); savePlayback(false, false); });
    audio.addEventListener("playing", () => {
      const t = M.queue[M.idx];
      if (t && !M.live) {
        $("#ny-np-sub").textContent = subOf(t); beginHistory(t);
        djBetween(t);
        const discoverIndex = M.discoverTracks.findIndex((item) => trackKey(item) === trackKey(t));
        if (M.tab === "discover" && discoverIndex >= 0 && discoverIndex !== M.discoverIndex) { M.discoverIndex = discoverIndex; renderTab(); }
      }
    });
    audio.addEventListener("pause", () => { historyProgress(true); paintBtn(); if (!M.stopping) savePlayback(false, true); });
    audio.addEventListener("error", onAudioError);
    $("#ny-play").onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
    $("#ny-prev").onclick = () => { if (!M.live) play(M.idx - 1); };
    $("#ny-next").onclick = () => { if (!M.live) next(); };
    $("#ny-close").onclick = stop;
    $("#ny-shuffle").onclick = () => { M.shuffle = !M.shuffle; resetShuffle(M.idx); paintBtn(); savePlayback(false, true); };
    $("#ny-repeat").onclick = cycleRepeat;
    $("#ny-copy").onclick = copyCurrentLink;
    $("#ny-rate").onchange = (e) => setPlaybackRate(e.target.value, true);
    $("#ny-eq").onchange = (e) => setEqualizer(e.target.value, true);
    $("#ny-seek").oninput = (e) => { if (audio.duration && M.seekable && !M.live) audio.currentTime = (e.target.value / 100) * audio.duration; };
    $("#ny-vol").oninput = (e) => { audio.volume = +e.target.value; if (audio.muted && audio.volume > 0) audio.muted = false; paintVolume(); savePlayback(false, false); };
    $("#ny-vol-i").onclick = toggleMute;
    $("#ny-sleep").onclick = cycleSleep;
    audio.addEventListener("volumechange", () => { paintVolume(); notify(); });
    setInterval(sleepTick, 1000);
    mediaHandlers();
  }

  // ---- volume, mute and the sleep timer ------------------------------------
  function paintVolume() {
    const i = $("#ny-vol-i"); if (!i || !audio) return;
    const quiet = audio.muted || audio.volume === 0;
    i.textContent = quiet ? "🔇" : audio.volume < 0.5 ? "🔉" : "🔊";
    i.title = quiet ? "Unmute" : "Mute"; i.setAttribute("aria-label", i.title);
    const v = $("#ny-vol"); if (v && Math.abs(+v.value - audio.volume) > 0.001) v.value = audio.volume;
  }
  // Loop: Off → Song → Playlist → Off (the same order everywhere the player shows it)
  function cycleRepeat() { ensureBar(); M.repeat = M.repeat === "off" ? "one" : M.repeat === "one" ? "all" : "off"; paintBtn(); savePlayback(false, true); }
  function toggleMute() { if (!audio) return; audio.muted = !audio.muted; if (!audio.muted && audio.volume === 0) audio.volume = 0.5; paintVolume(); }
  function setVolume(v) { ensureBar(); audio.volume = Math.max(0, Math.min(1, +v || 0)); if (audio.volume > 0) audio.muted = false; paintVolume(); savePlayback(false, false); }
  const SLEEP_STEPS = [null, 15, 30, 60, "end"];
  function sleepLabel() {
    if (!M.sleep) return "";
    if (M.sleep === "end") return "end of song";
    return Math.max(1, Math.ceil((M.sleep - Date.now()) / 60000)) + " min";
  }
  function cycleSleep() {
    const now = M.sleep === "end" ? "end" : M.sleep ? Math.round((M.sleep - Date.now()) / 60000) : null;
    const at = SLEEP_STEPS.findIndex((x) => x === now || (typeof x === "number" && typeof now === "number" && now <= x));
    const nextStep = SLEEP_STEPS[(Math.max(0, at) + 1) % SLEEP_STEPS.length];
    M.sleep = nextStep === null ? null : nextStep === "end" ? "end" : Date.now() + nextStep * 60000;
    paintSleep(); notify();
    toast(M.sleep ? "Sleep timer: music stops " + (M.sleep === "end" ? "after this song." : "in " + nextStep + " minutes.") : "Sleep timer off.");
  }
  function paintSleep() {
    const b = $("#ny-sleep"), st = $("#ny-sleep-state"); if (!b) return;
    const label = sleepLabel();
    b.classList.toggle("act", !!M.sleep);
    b.title = "Sleep timer: " + (label || "off") + " (click to change)"; b.setAttribute("aria-label", b.title);
    if (st) { st.textContent = label; st.classList.toggle("on", !!label); }
  }
  function sleepTick() {
    if (typeof M.sleep !== "number") return;
    if (Date.now() >= M.sleep) { M.sleep = null; if (audio && !audio.paused) audio.pause(); paintSleep(); notify(); toast("Sleep timer: the music is paused. Good night."); }
    else paintSleep();
  }

  // ---- lock screen, media keys and headphone buttons ------------------------------
  let sessionKey = null;
  function mediaHandlers() {
    if (!("mediaSession" in navigator)) return;
    const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch (e) { /* not supported here */ } };
    set("play", () => { if (audio) audio.play().catch(() => {}); });
    set("pause", () => { if (audio) audio.pause(); });
    set("stop", () => stop());
    set("previoustrack", () => { if (!M.live && M.idx > 0) play(M.idx - 1); });
    set("nexttrack", () => { if (!M.live && M.queue[M.idx]) next(); });
    set("seekbackward", (d) => { if (audio && M.seekable && !M.live) audio.currentTime = Math.max(0, audio.currentTime - ((d && d.seekOffset) || 10)); });
    set("seekforward", (d) => { if (audio && M.seekable && !M.live && audio.duration) audio.currentTime = Math.min(audio.duration - 0.5, audio.currentTime + ((d && d.seekOffset) || 10)); });
    set("seekto", (d) => { if (audio && M.seekable && !M.live && d && d.seekTime != null) audio.currentTime = d.seekTime; });
  }
  function mediaSession() {
    if (!("mediaSession" in navigator) || !audio) return;
    try {
      const t = M.live ? null : M.queue[M.idx], key = M.live ? "live" : t ? trackKey(t) : "";
      if (key !== sessionKey) {
        sessionKey = key;
        const art = t ? artworkSource(t) : "";
        navigator.mediaSession.metadata = !key ? null : new MediaMetadata({ title: M.live ? "Server audio" : titleOf(t), artist: M.live ? "Live from the host" : subOf(t),
          album: "nodeyard", artwork: art ? [{ src: new URL(art, location.href).href, sizes: "512x512" }] : [] });
      }
      navigator.mediaSession.playbackState = !key ? "none" : audio.paused ? "paused" : "playing";
      if (t && audio.duration && isFinite(audio.duration) && "setPositionState" in navigator.mediaSession) {
        navigator.mediaSession.setPositionState({ duration: audio.duration, playbackRate: audio.playbackRate || 1, position: Math.min(audio.currentTime || 0, audio.duration) });
      }
    } catch (e) { /* metadata is a nicety */ }
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
        eqNodes = [60, 250, 1000, 4000, 10000].map((frequency, i) => {
          const filter = actx.createBiquadFilter();
          filter.type = i === 0 ? "lowshelf" : i === 4 ? "highshelf" : "peaking";
          filter.frequency.value = frequency;
          if (filter.type === "peaking") filter.Q.value = 0.9;
          return filter;
        });
        let previous = vizSource;
        eqNodes.forEach((filter) => { previous.connect(filter); previous = filter; });
        previous.connect(analyser);
        analyser.connect(actx.destination);
        setEqualizer(M.eq, false);
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
  // the accent colour, read every couple of seconds instead of every frame (reading it forces a style recalculation)
  let vizColor = "#ff5500", vizColorAt = 0, vizLast = 0;
  function accent() {
    const now = performance.now();
    if (now - vizColorAt > 2000) { vizColorAt = now; vizColor = (getComputedStyle(document.documentElement).getPropertyValue("--sc") || "#ff5500").trim() || "#ff5500"; }
    return vizColor;
  }
  function drawBars(cv, bars) {
    const ctx = cv.getContext("2d"); const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    const step = Math.floor(vizData.length / bars) || 1, bw = w / bars;
    const col = accent();
    for (let i = 0; i < bars; i++) {
      let v = 0; for (let j = 0; j < step; j++) v += vizData[i * step + j] || 0; v /= step;
      const bh = Math.max(2, (v / 255) * h);
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.3 + 0.7 * (v / 255);
      ctx.fillRect(i * bw + 1, h - bh, Math.max(1, bw - 2), bh);
    }
    ctx.globalAlpha = 1;
  }
  function drawViz(now) {
    vizRAF = requestAnimationFrame(drawViz);
    if (!analyser) return;
    if (now && now - vizLast < 32) return;   // ~30 frames a second is plenty for bars, and half the work
    vizLast = now || 0;
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
  function setPlaybackRate(value, persist) {
    const rate = Math.max(0.5, Math.min(2, Number(value) || 1));
    M.rate = rate;
    if (audio) {
      audio.defaultPlaybackRate = rate;
      audio.playbackRate = rate;
      audio.preservesPitch = true;
      audio.webkitPreservesPitch = true;
    }
    const select = $("#ny-rate"); if (select) select.value = String(rate);
    if (persist) savePlayback(false, true);
  }
  function setEqualizer(mode, persist) {
    M.eq = Object.prototype.hasOwnProperty.call(EQ_PRESETS, mode) ? mode : "flat";
    const gains = EQ_PRESETS[M.eq];
    eqNodes.forEach((filter, i) => filter.gain.setTargetAtTime(gains[i], actx.currentTime, 0.035));
    const select = $("#ny-eq"); if (select) select.value = M.eq;
    if (persist) savePlayback(false, true);
  }
  function tick() {
    if (!bar) return;
    $("#ny-cur").textContent = fmtTime(audio.currentTime);
    const known = M.idx >= 0 && M.queue[M.idx] && M.queue[M.idx].duration_ms ? M.queue[M.idx].duration_ms / 1000 : audio.duration;
    $("#ny-dur").textContent = M.live ? "live" : fmtTime(known);
    const sk = $("#ny-seek");
    sk.disabled = M.live || !M.seekable;
    if (M.seekable && !M.live && audio.duration) sk.value = (audio.currentTime / audio.duration) * 100;
    if (!M.lastSession || Date.now() - M.lastSession > 5000) { M.lastSession = Date.now(); mediaSession(); }
    notify();
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
    mediaSession(); paintSleep();
    notify();
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
    const key = trackKey(track);
    if (key) {
      M.homeSessionHeard.add(key);
      if (M.tab === "home") renderTab();
    }
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
      rate: audio.playbackRate || M.rate,
      eq: M.eq,
      history_id: M.historyId, history_seconds: history ? history.seconds : M.historyElapsed, handoff: !!handoff };
    if (!keepalive && Date.now() - M.lastPlaybackSave < 15000) return;
    M.lastPlaybackSave = Date.now();
    if (keepalive) {
      fetch("/api/music/playback", { method: "POST", keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify(body) }).catch(() => {});
    } else postJSON("/api/music/playback", body).catch(() => {});
  }
  // force: "press play" with nothing loaded: pick up the last saved queue however old it is, and play it.
  async function restorePlayback(force) {
    if (M.restored && !force) return false;
    M.restored = true;
    try {
      const r = await getJSON("/api/music/playback");
      const state = r.playback || {};
      // The native app replaces the Music WKWebView when the user selects AI Galaxy, so pagehide is not guaranteed.
      // Restore any recent player state; the `playing` flag still keeps paused tracks paused.
      if (!r.ok || !Array.isArray(state.queue) || !state.queue.length || (!force && Date.now() / 1000 - (+state.updated || 0) > 180)) return false;
      ensureBar();
      M.queue = state.queue; M.idx = Math.max(0, Math.min(+state.index || 0, M.queue.length - 1));
      M.shuffle = !!state.shuffle; M.repeat = ["off", "all", "one"].includes(state.repeat) ? state.repeat : "off";
      setPlaybackRate(state.rate, false);
      setEqualizer(state.eq, false);
      M.historyId = state.history_id || ""; M.historyElapsed = Math.max(0, +state.history_seconds || 0); M.historySince = 0;
      const track = M.queue[M.idx]; if (!track) return;
      if (M.shuffle) resetShuffle(M.idx);
      audio.volume = Math.max(0, Math.min(1, +state.volume || 0));
      audio.src = srcFor(track);
      const resumeAt = Math.max(0, +state.position || 0);
      audio.addEventListener("loadedmetadata", () => { if (resumeAt < audio.duration) audio.currentTime = resumeAt; }, { once: true });
      $("#ny-np-title").textContent = titleOf(track); $("#ny-np-sub").textContent = subOf(track) + " · resuming…";
      $("#ny-vol").value = audio.volume; bar.classList.add("on"); paintBtn();
      if (state.playing || force) audio.play().catch(() => { $("#ny-np-sub").textContent = subOf(track) + " · press play to resume"; });
      savePlayback(false, true);
      return true;
    } catch (e) { /* music state is optional */ }
    return false;
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
    setPlaybackRate(M.rate, false);
    const sc = (t.kind === "sc" || t.url);
    M.seekable = true;     // the server buffers both local files and SoundCloud, so seeking & resume work
    audio.src = srcFor(t);
    audio.play().then(() => savePlayback(false, true)).catch((e) => {
      if (e && e.name === "NotAllowedError") toast("Playback was blocked. Press Play to start the songs.");
    });
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
  // What plays after this one, without moving anything (null when nothing follows).
  function peekNext() {
    if (M.live || !M.queue.length || M.repeat === "one") return null;
    if (M.shuffle && M.queue.length > 1) return M.shuffleOrder && M.shufflePos < M.shuffleOrder.length ? M.queue[M.shuffleOrder[M.shufflePos]] : null;
    return M.queue[M.idx + 1] || (M.repeat === "all" ? M.queue[0] : null);
  }
  // A SoundCloud song takes the server several seconds to fetch and convert: near the end of this one, ask for the next,
  // so it starts at once (the server keeps it in memory; this only reads its first two bytes).
  function prefetchNext() {
    if (!audio || !audio.duration || audio.duration - audio.currentTime > 30) return;
    const t = peekNext(); if (!t || !(t.kind === "sc" || t.url)) return;
    const key = trackKey(t); if (M.prefetched === key) return;
    M.prefetched = key;
    fetch(srcFor(t), { headers: { Range: "bytes=0-1" }, cache: "no-store", credentials: "same-origin" }).catch(() => { M.prefetched = ""; });
  }
  function onEnded() {
    if (M.live) return;
    historyProgress(true);
    const body = historyBody("complete"); if (body) postJSON("/api/music/history", body).catch(() => {});
    if (M.sleep === "end") { M.sleep = null; paintSleep(); notify(); toast("Sleep timer: stopped after that song. Good night."); return; }
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
    notify();
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
    const cover = artworkSource(t);
    return html`<div class="sc-row">
      <button class="sc-pbtn ${playing ? "" : "ghost"}" data-play="${onplay}" title="Play">${playing ? "⏸" : "▶"}</button>
      <div class="sc-cover" aria-hidden="true">♪${cover ? html`<img src="${cover}" alt="" loading="lazy">` : ""}</div>
      <div class="meta"><div><b>${titleOf(t)}</b></div><div class="faint small">${subOf(t)}${dur ? " · " + dur : ""}</div>${t.why ? html`<div class="sc-why">${t.why}</div>` : ""}</div>
      ${extra || ""}
      <button class="btn small" data-music="add" data-key="${trackKey(t)}" title="Add to a playlist">＋</button>
    </div>`;
  }

  // ---- the local AI DJ ----------------------------------------------------
  function saveDJ() {
    try {
      localStorage.setItem("ny-music-dj", JSON.stringify({ active: !!M.dj.active, target: M.dj.target || "", voice: M.dj.voice || "",
        speak: M.dj.speak !== false, between: M.dj.between !== false, messages: (M.dj.messages || []).slice(-16), draft: M.dj.draft || null }));
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
  // Natural voices: Piper on the server (djvoice.py). Picking one that isn't downloaded yet offers to get it.
  // (it redraws the DJ tab itself once the answer is in: a redraw must never ask for another one, or they'd loop)
  async function loadPiper(force) {
    if (M.dj.piperLoading || (!force && (M.dj.piper || Date.now() < (M.dj.piperRetry || 0)))) return;
    M.dj.piperLoading = true;
    try { const r = await getJSON("/api/music/dj-voice"); if (r.ok) M.dj.piper = r; else M.dj.piperRetry = Date.now() + 60000; }
    catch (e) { M.dj.piperRetry = Date.now() + 60000; }
    M.dj.piperLoading = false;
    if (S.view === "music" && M.tab === "dj") renderTab();
    if (M.dj.piper && M.dj.piper.busy) setTimeout(() => loadPiper(true), 2000);
  }
  const piperVoice = () => (String(M.dj.voice || "").startsWith("piper:") ? M.dj.voice.slice(6) : "");
  function djVoiceOptions() {
    const voices = djVoices().slice().sort((a, b) => djVoiceScore(b) - djVoiceScore(a)), best = djBestVoice();
    const piper = (M.dj.piper && M.dj.piper.voices) || [];
    const natural = piper.length ? '<optgroup label="Natural voices (on the server)">' + piper.map((v) => '<option value="piper:' + esc(v.id) + '"' +
      ("piper:" + v.id === M.dj.voice ? " selected" : "") + ">" + esc(v.name + (v.ready ? "" : " · download (63 MB)")) + "</option>").join("") + "</optgroup>" : "";
    return natural + '<optgroup label="This browser\'s voices"><option value=""' + (M.dj.voice ? "" : " selected") + ">Automatic" + (best ? ": " + esc(best.name) : " (browser default)") + "</option>" +
      voices.map((v) => '<option value="' + esc(v.name) + '"' + (v.name === M.dj.voice ? " selected" : "") + ">" + esc(v.name + " (" + v.lang + ")" + (djVoiceScore(v) >= 135 ? " · natural" : "")) + "</option>").join("") + "</optgroup>";
  }
  function piperNote() {
    const P = M.dj.piper, id = piperVoice();
    if (!P) return "";
    if (P.busy) return html`<div class="small mt"><span class="spin small"></span> Setting up the natural voice: ${P.busy}…</div>`;
    if (P.error) return html`<div class="small mt bad-t">${P.error}</div>`;
    const v = id && (P.voices || []).find((x) => x.id === id);
    if (v && !v.ready) return html`<div class="row wrap mt" style="gap:8px;align-items:center"><span class="small">${v.name} isn't on the server yet.</span><button class="btn small primary" data-music="dj-get-voice">Download it (about ${P.installed ? "63" : "90"} MB)</button></div>`;
    if (!id && !P.demo) return html`<div class="faint small mt">For a voice that sounds like a person, pick one of the natural voices: it's made on your server by Piper, not by the browser.</div>`;
    return "";
  }

  // what the DJ says, made speakable: no emoji, links or markdown, and song titles without their clutter
  function speechTitle(t) {
    return String(t || "").replace(/\[[^\]]*\]/g, " ").replace(/\((?:prod|feat|ft|free|official|video|audio|lyric|out now|preview|clip)[^)]*\)/gi, " ")
      .replace(/\*[^*]*\*/g, " ").replace(/\b(?:free download|out now|preview|official (?:video|audio))\b/gi, " ").replace(/[|#@]+/g, " ")
      .replace(/\p{Extended_Pictographic}|\uFE0F/gu, " ").replace(/\s+[-–—]\s+/g, ", ")
      // (voices spell out words in capitals: HAVA NAGILA -> Hava Nagila; short ones like DJ or UK stay)
      .replace(/\b\p{Lu}{4,}\b/gu, (w) => w[0] + w.slice(1).toLowerCase())
      .replace(/\s+/g, " ").replace(/^[\s\-–—:,]+|[\s\-–—:,]+$/g, "").trim() || String(t || "");
  }
  function speechText(text) {
    let t = String(text || "").replace(/https?:\/\/\S+/g, "a link").replace(/[*_`#>~]/g, "").replace(/\p{Extended_Pictographic}|\uFE0F/gu, "")
      .replace(/\s+&\s+/g, " and ").replace(/\bfeat\.?\s/gi, "featuring ").replace(/\bft\.?\s/gi, "featuring ").replace(/\bw\/\s?/gi, "with ")
      .replace(/\s*[—–]\s*/g, ", ").replace(/\.{3,}|…/g, "... ").replace(/\s+/g, " ").trim();
    if (t && !/[.!?]$/.test(t)) t += ".";
    return t;
  }
  // the music drops while the DJ talks, and comes back up after, like on the radio
  function duck(on) {
    if (!audio) return;
    clearInterval(M.dj.duckTimer);
    if (on) { if (M.dj.ducked == null) M.dj.ducked = audio.volume; }
    else if (M.dj.ducked == null) return;
    const target = on ? Math.min(M.dj.ducked, 0.22) : M.dj.ducked, start = audio.volume, t0 = performance.now(), ms = on ? 350 : 900;
    M.dj.duckTimer = setInterval(() => {
      const k = Math.min(1, (performance.now() - t0) / ms);
      audio.volume = start + (target - start) * k;
      if (k >= 1) { clearInterval(M.dj.duckTimer); if (!on) M.dj.ducked = null; }
    }, 30);
  }
  async function speakDJ(text) {
    if (M.dj.speak === false) return;
    const clean = speechText(text);
    if (!clean) return;
    stopDJVoice();
    const seq = M.dj.sayingSeq = (M.dj.sayingSeq || 0) + 1;
    const pv = piperVoice(), P = M.dj.piper;
    if (pv && P && (P.voices || []).some((v) => v.id === pv && v.ready)) {
      try {
        const r = await fetch("/api/music/dj-say", { method: "POST", cache: "no-store", credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, body: JSON.stringify({ text: clean, voice: pv }) });
        if (r.ok && seq === M.dj.sayingSeq) {
          const url = URL.createObjectURL(await r.blob());
          const a = M.dj.voiceAudio = new Audio(url);
          const done = () => { URL.revokeObjectURL(url); if (M.dj.voiceAudio === a) { M.dj.voiceAudio = null; duck(false); } };
          a.onended = done; a.onerror = done;
          duck(true);
          await a.play();
          return;
        }
      } catch (e) { /* the browser's voice below */ }
      if (seq !== M.dj.sayingSeq) return;
    }
    if (typeof speechSynthesis === "undefined" || typeof SpeechSynthesisUtterance === "undefined") return;
    const voice = djVoices().find((v) => v.name === M.dj.voice) || djBestVoice();
    // a sentence per utterance: the browser then pauses between them as a speaker would (and long ones don't get cut off)
    const parts = clean.match(/[^.!?]+[.!?]+["”']?|\S[^.!?]*$/g) || [clean];
    parts.forEach((part, i) => {
      const u = new SpeechSynthesisUtterance(part.trim());
      if (voice) { u.voice = voice; u.lang = voice.lang; }
      u.rate = 1.0 + (i % 2 ? 0.03 : -0.02); u.pitch = 1.0 + (i === 0 ? 0.04 : 0);   // (a little variation sounds less read-out)
      if (i === 0) u.onstart = () => duck(true);
      if (i === parts.length - 1) { u.onend = () => duck(false); u.onerror = () => duck(false); }
      speechSynthesis.speak(u);
    });
  }
  function stopDJVoice() {
    if (M.dj.voiceAudio) { M.dj.voiceAudio.pause(); M.dj.voiceAudio = null; }
    if (typeof speechSynthesis !== "undefined") speechSynthesis.cancel();
    duck(false);
  }
  // between songs of a DJ set, now and then, like a radio host: a line made here, so it's instant (no AI wait)
  function djBetween(t) {
    if (M.dj.speak === false || M.dj.between === false || !M.dj.active || !M.dj.draft || !t) return;
    const set = M.dj.draft.tracks || [], i = set.findIndex((x) => trackKey(x) === trackKey(t));
    if (i <= 0 || M.dj.lastIntro === trackKey(t)) return;
    M.dj.lastIntro = trackKey(t);
    const fresh = !!t.why, n = M.dj.talkCount = (M.dj.talkCount || 0) + 1;
    if (!fresh && n % 3 !== 1) return;                    // (not after every song: new finds always, others every third)
    const prev = set[i - 1], title = speechTitle(titleOf(t)), who = speechTitle(t.artist || ""), ptitle = speechTitle(titleOf(prev)), pwho = speechTitle(prev.artist || "");
    const pick = (list) => list[Math.floor(Math.random() * list.length)];
    const by = who ? " by " + who : "", from = who ? " from " + who : "";
    const line = fresh ? pick(["Here's one you haven't heard yet: " + title + by + ".", "Something new for you now. This is " + title + by + ".",
      "Fresh one coming up, " + title + by + ". Tell me if it's a keeper.", "You've never heard this one. " + title + from + "."])
      : pick(["That was " + ptitle + (pwho ? " from " + pwho : "") + ". Now, " + title + by + ".", title + by + ", right here.",
        "Keeping it going with " + title + by + ".", "Love that one. Up next, " + title + by + "."]);
    speakDJ(line);
  }
  function previewDJVoice() {
    if (piperVoice()) { const v = ((M.dj.piper || {}).voices || []).find((x) => x.id === piperVoice()); if (v && !v.ready) { toast("Download that voice first."); return; } }
    if (!piperVoice() && (typeof speechSynthesis === "undefined" || typeof SpeechSynthesisUtterance === "undefined")) { toast("Voice preview isn't available in this browser."); return; }
    speechSynthesis.cancel();
    if (piperVoice()) { speakDJ("Hey, it's your Nodeyard DJ. Tell me what you're in the mood for, and I'll line up the songs."); return; }
    const utterance = new SpeechSynthesisUtterance("Hey, it’s your Nodeyard DJ. Tell me what you’re in the mood for, and I’ll line up the songs.");
    const voice = djVoices().find((v) => v.name === M.dj.voice) || djBestVoice();
    if (voice) { utterance.voice = voice; utterance.lang = voice.lang; }
    utterance.rate = 0.96; utterance.pitch = 1.02;
    utterance.onerror = () => toast("Couldn't preview that voice.");
    speechSynthesis.speak(utterance);
  }
  function djTargets() {
    if (M.dj.targets === null) loadDJTargets();
    const ready = ((M.dj.targets || {}).targets || []).filter((x) => x.ready);
    const keep = M.dj.target;
    return ready.length
      ? ready.map((x) => '<option value="' + esc(x.id) + '"' + (x.id === keep ? " selected" : "") + ">" + esc(x.name + (x.kind === "ollama" ? " · " + (x.detail || "Ollama") : " · split model")) + "</option>").join("")
      : '<option value="">' + (M.dj.targets ? "No model is ready" : "Loading models…") + "</option>";
  }
  // Which models can answer: asked again every 30 s, and every 5 s while none can (a model that finishes loading
  // while the page is open shows up by itself; asking only once left the DJ saying "No model is ready" for good).
  // It redraws the tab itself; a redraw never asks again (that would loop).
  async function loadDJTargets(force) {
    if (M.dj.loadingTargets || (!force && M.dj.targets !== null && Date.now() - (M.dj.targetsAt || 0) < 30000)) return;
    M.dj.loadingTargets = true;
    let ready = [];
    try {
      const r = await getJSON("/api/ai/targets");
      M.dj.targets = r.ok ? r : { targets: [], error: r.error || "Couldn't load the AI models." };
      ready = (r.targets || []).filter((x) => x.ready);
      if (!ready.some((x) => x.id === M.dj.target)) M.dj.target = ready.length ? ready[0].id : "";
    } catch (e) { M.dj.targets = { targets: [], error: e.message || "Couldn't load the AI models." }; }
    M.dj.loadingTargets = false;
    M.dj.targetsAt = Date.now();
    clearTimeout(M.dj.targetsTimer);
    if (S.view === "music" && M.tab === "dj") {
      renderTab();
      if (!ready.length) M.dj.targetsTimer = setTimeout(() => loadDJTargets(true), 5000);
    }
  }
  function renderDJ(el) {
    const ready = ((M.dj.targets || {}).targets || []).some((x) => x.ready);
    const voiceNote = typeof speechSynthesis === "undefined"
      ? "Speech isn't available in this browser; the DJ will still answer in text."
      : "Uses your browser's best available voice. Enhanced, Natural, Neural or Premium voices sound most lifelike when installed.";
    if (!M.dj.active) {
      setHTML(el, html`<div class="card">
        <div class="row" style="justify-content:space-between;align-items:center;gap:10px"><div><h3 style="margin:0">Nodeyard DJ</h3><div class="muted small mt">A private, local AI host for your music. Ask for a vibe, activity, or custom playlist.</div></div><span class="chip">Ready when you are</span></div>
        <p class="small mt">Likes help set the mood. For normal requests, the DJ also picks unliked songs from your library and listening history; ask for your liked songs when you want a likes-only mix.</p>
        <div class="row wrap mt" style="gap:10px;align-items:flex-end">
          <label class="small">AI model<select class="select mt" data-music="dj-target" ${ready ? "" : "disabled"}>${raw(djTargets())}</select></label>
          <label class="small">Voice<select class="select mt" data-music="dj-voice">${raw(djVoiceOptions())}</select></label><button class="btn" data-music="dj-preview-voice">▶ Preview voice</button>
          <button class="btn primary" data-music="dj-start" ${ready ? "" : "disabled"}>▶ Start DJ</button>
        </div>${piperNote()}
        <div class="faint small mt">${voiceNote}</div>
        ${M.dj.targets && !ready ? html`<p class="note warn small mt">${M.dj.targets.error || "No AI model is ready. Start one under AI → Models, then come back here."}</p>` : ""}
      </div>`);
      if (M.dj.targets === null) loadDJTargets();
      if (!M.dj.piper) loadPiper();
      return;
    }
    if (!M.dj.piper) loadPiper();
    const messages = (M.dj.messages || []).filter((m) => !m.hidden);
    setHTML(el, html`<div class="card">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h3 style="margin:0">Nodeyard DJ <span class="chip good">${M.dj.busy ? "thinking" : "on air"}</span></h3><div class="faint small mt">Ask for a mood, moment, or playlist. The DJ knows your likes and recent plays.</div></div>
        <button class="btn small" data-music="dj-stop">Stop DJ</button></div>
      <div class="row wrap mt" style="gap:8px;align-items:flex-end">
        <label class="small">AI model<select class="select mt" data-music="dj-target">${raw(djTargets())}</select></label>
        <label class="small">Voice<select class="select mt" data-music="dj-voice">${raw(djVoiceOptions())}</select></label><button class="btn" data-music="dj-preview-voice">▶ Preview voice</button>
          <label class="check small"><input type="checkbox" data-music="dj-speak" ${M.dj.speak !== false ? raw("checked") : ""}> <span>Speak replies</span></label>
          <label class="check small"><input type="checkbox" data-music="dj-between" ${M.dj.between !== false ? raw("checked") : ""}> <span>Talk between songs</span></label>
      </div>${piperNote()}
      <div id="dj-chat" style="max-height:380px;overflow:auto;display:flex;flex-direction:column;gap:10px;margin-top:14px">
        ${messages.map((m) => html`<div style="align-self:${m.role === "user" ? "flex-end" : "flex-start"};max-width:min(88%,720px);padding:10px 13px;border-radius:14px;background:${m.role === "user" ? "rgba(255,85,0,.16)" : "var(--panel2,rgba(125,160,200,.09))"}"><div class="small">${m.role === "assistant" ? "🎙 DJ" : "You"}</div><div style="white-space:pre-wrap;margin-top:3px">${m.content}</div></div>`)}
        ${M.dj.busy ? html`<div class="empty"><div class="spin"></div>${M.dj.status || "DJ is thinking…"}</div>` : ""}
      </div>
      ${M.dj.draft && M.dj.draft.tracks && M.dj.draft.tracks.length ? html`<div class="mt" style="border-top:1px solid var(--line);padding-top:12px">
        <div class="row wrap" style="justify-content:space-between;align-items:center;gap:8px"><b>DJ set: ${M.dj.draft.name} <span class="faint small">(${M.dj.draft.tracks.length} tracks)</span></b>
          <div class="row" style="gap:6px"><button class="btn small primary" data-music="dj-play">▶ Play set</button><button class="btn small" data-music="dj-save" ${M.dj.draft.saved ? "disabled" : ""}>${M.dj.draft.saved ? "Saved ✓" : "Save playlist"}</button></div></div>
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
    const actions = ["liked", "history"];
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
    const localTracks = Array.isArray((M.data || {}).tracks) ? M.data.tracks : [];
    localTracks.forEach((track) => add(track, "library"));
    if (!localTracks.length) {
      const page = await getJSON("/api/music/ai?action=library&offset=0&limit=50").catch(() => ({ ok: false, tracks: [] }));
      (page.tracks || []).forEach((track) => add(track, "library"));
    }
    const all = Array.from(map.values());
    const spread = (rows, limit) => {
      if (rows.length <= limit) return rows;
      const out = [], step = rows.length / limit;
      for (let i = 0; i < limit; i++) out.push(rows[Math.floor((i + .5) * step)]);
      return out;
    };
    const selected = [], seen = new Set();
    const append = (rows, limit) => spread(rows, Math.max(0, limit)).forEach((item) => {
      const key = item.track.url || item.track.path;
      if (key && !seen.has(key) && selected.length < 150) { seen.add(key); selected.push(item); }
    });
    // new songs found for this listener (not heard yet): up to 20 of the 150 places, so the prompt stays the same size
    if (!M.disc.loaded) await loadDiscover(false);
    const fresh = discovered().filter((t) => !map.has(t.url)).slice(0, 20).map((t) => ({ track: t, sources: new Set(["new"]), plays: 0, lastPlayed: 0 }));
    append(fresh, 20);
    const unliked = all.filter((item) => !item.sources.has("liked"));
    const liked = all.filter((item) => item.sources.has("liked"));
    const library = unliked.filter((item) => item.sources.has("library"));
    const historyOnly = unliked.filter((item) => !item.sources.has("library") && item.sources.has("history"));
    append(library, 100 - fresh.length);
    append(historyOnly, 25);
    append(liked, 25);
    if (selected.length < 150) append(unliked.filter((item) => !seen.has(item.track.url || item.track.path)), 150 - selected.length);
    if (selected.length < 150) append(liked.filter((item) => !seen.has(item.track.url || item.track.path)), 150 - selected.length);
    return selected.map((item, i) => {
      const t = item.track, local = !t.url;
      return { id: "t" + String(i + 1).padStart(3, "0"), track: { ...t, kind: local ? "local" : "sc" },
        info: { id: "t" + String(i + 1).padStart(3, "0"), title: local ? t.name : t.title, artist: local ? (t.folder || "your library") : (t.artist || "SoundCloud"),
          sources: Array.from(item.sources), liked: item.sources.has("liked"), from_library: item.sources.has("library"),
          in_listening_history: item.sources.has("history"), plays: item.plays, last_played: item.lastPlayed || undefined,
          new_to_you: item.sources.has("new") || undefined, why: item.sources.has("new") ? t.why : undefined } };
    });
  }
  // what to look for on SoundCloud for one request: its words, minus the filler ("make me a ... mix")
  const DJ_FILLER = new Set(("a an the me my mine i im i'm ive i've want wanna would like could can you please pls give gimme gimmie lemme get make create build put together play " +
    "some something mix mixes playlist playlists set songs song tracks track music tunes of with for to and that this kind sort type bit little " +
    "vibe vibes vibey mood moody new fresh different discover more stuff thing things just maybe really hi hello hey thanks thank ok okay yes yeah " +
    "no nope cool nice great good what do does did think how why who are is it its it's was be that's again now too also so than then").split(" "));
  function djSearchQuery(text) {
    const t = String(text || "");
    // a question with no music words in it ("what do you think?") isn't a request for songs
    if (/\?\s*$/.test(t) && !/\b(?:mix|playlist|songs?|tracks?|music|play|vibes?)\b/i.test(t)) return "";
    const words = t.toLowerCase().replace(/[^\p{L}\p{N}' -]+/gu, " ").split(/\s+/).filter((w) => w.length > 1 && !DJ_FILLER.has(w));
    if (!words.length) return "";
    return words.length === 1 ? words[0] + " music" : words.slice(0, 6).join(" ");
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
    const candidates = [clean, clean.slice(first, last + 1)];
    for (const candidate of candidates) {
      try {
        let obj = JSON.parse(candidate);
        if (typeof obj === "string") obj = JSON.parse(obj);
        if (obj && typeof obj === "object" && !Array.isArray(obj) && (typeof obj.speech === "string" || typeof obj.response === "string")) return obj;
      } catch (e) { /* try the extracted object or speech field */ }
    }
    const speechField = /"speech"\s*:\s*("(?:\\.|[^"\\])*")/.exec(clean);
    if (speechField) {
      try { return { speech: JSON.parse(speechField[1]), playlist_name: "", track_ids: [] }; } catch (e) { /* show the original response */ }
    }
    return { speech: clean, playlist_name: "", track_ids: [] };
  }
  function readableDJMessage(content) {
    const text = String(content || ""), clean = text.trim();
    if (!/^\s*(?:```|\{)/.test(clean)) return text;
    const parsed = parseDJAnswer(clean), speech = String(parsed.speech || parsed.response || "").trim();
    if (!speech || speech === clean) return text;
    const ids = Array.isArray(parsed.track_ids) ? parsed.track_ids : [];
    const name = String(parsed.playlist_name || "").trim();
    return speech + (ids.length ? "\n\nI put together “" + (name || "Your mix") + "” with " + ids.length + " tracks." : "");
  }
  async function sendDJRequest(prompt, hidden) {
    if (M.dj.busy) return;
    if (!M.dj.target) { toast("Choose a ready AI model first."); return; }
    const userText = String(prompt || "").trim().slice(0, 4000); if (!userText) return;
    if (!hidden) M.dj.messages.push({ role: "user", content: userText });
    M.dj.busy = true; M.dj.error = "";
    const controller = new AbortController(); M.dj.controller = controller; saveDJ(); renderTab();
    try {
      // one catalogue per DJ session (30 min): the same system prompt every turn lets the model keep it read
      const fresh = M.dj.catalog && Date.now() - M.dj.catalog.at < 30 * 60000;
      const catalog = fresh ? M.dj.catalog.rows : await djCatalog();
      if (!fresh) M.dj.catalog = { at: Date.now(), rows: catalog };
      if (!catalog.length) throw new Error("There are no songs for the DJ yet. Set a Music folder, like some SoundCloud songs, or listen to a few tracks first.");
      const catalogText = catalog.map((x) => x.info);
      // songs for this request the listener has never heard: searched on SoundCloud now, sent with this message only
      // (the catalogue in the system prompt stays the same all session, so the model keeps it read)
      const asksForLikes = /\b(?:liked songs|my likes|from my likes|favorites?|favourites?|songs?\s+(?:that\s+)?i\s+(?:already\s+)?liked|songs?\s+i(?:'|’)ve liked)\b/i.test(userText);
      const wantsNew = /\b(?:new|fresh|discover|different|haven(?:'|’)?t heard|never heard|explore|surprise)\b/i.test(userText);
      const wantsKnown = asksForLikes || /\b(?:songs? i know|familiar|old favou?rites?|my songs|my library|already (?:heard|listened))\b/i.test(userText);
      let searchRows = [];
      let query = hidden || wantsKnown ? "" : djSearchQuery(userText);
      if (!query && !hidden && !wantsKnown && M.dj.lastQuery && /\b(?:more|another|similar|again|like (?:that|this|those))\b/i.test(userText)) query = M.dj.lastQuery;   // "more like that"
      if (query) M.dj.lastQuery = query;
      if (query) {
        M.dj.status = "Finding new songs for that on SoundCloud…"; renderTab();
        try {
          // (the server reads moods and activities, "to code to", "for the gym", as a mood for your own styles)
          const sr = await postJSON("/api/music/dj-search", { text: userText, query });
          const known = new Set(catalog.map((x) => trackKey(x.track)));
          searchRows = (sr.tracks || []).filter((t) => !known.has(trackKey({ ...t, kind: "sc" }))).map((t, i) => {
            const id = "n" + String(i + 1).padStart(3, "0");
            return { id, track: { ...t, kind: "sc" }, info: { id, title: t.title, artist: t.artist || "SoundCloud", new_to_you: true, why: t.why || "found for this request" } };
          });
        } catch (e) { /* the catalogue alone still makes a mix */ }
        M.dj.status = ""; renderTab();
      }
      const system = "You are Nodeyard DJ, a warm, confident, conversational music host. Sound like a natural radio presenter: short, personal, lightly playful, never overdo catchphrases. Your speech is read aloud by a voice, so write it the way people talk: contractions, everyday words, a mix of short and longer sentences, at most one exclamation, no emoji, hashtags, lists, brackets or stage directions, and don't introduce yourself again after the first time. When you make a mix, name one or two of its songs or artists the way a host would (\"opening with Night Run from Kollapse\"), and don't read out track counts. Use listening history and likes to personalize, but never claim you listened to audio or know lyrics unless given. Recommend only catalogue tracks. The user request may be a vibe, activity, artist/style preference, or follow-up. If you need one key detail, ask one concise question and return no tracks. Otherwise create a useful mix. IMPORTANT: for ordinary vibe/activity requests, use unliked tracks from the user's library and listening history first; likes are taste context, not the default source. Never make a mix only from liked tracks when unliked catalogue tracks are available. Include at least half unliked tracks where possible. Only focus on liked tracks when the user explicitly asks for their likes/favourites. Catalogue fields `liked`, `from_library`, and `in_listening_history` tell you which tracks are liked or unliked. Tracks with `new_to_you: true` are songs the user has never heard, picked for their taste (`why` says why): put about a quarter of them into ordinary mixes so the user discovers music, and lead with them (at least half) when the user asks for something new, fresh, different or to discover; skip them only when the user asks for songs they know. A request may come with NEW SONGS FOR THIS REQUEST (ids n001...): songs matching what the user asked for that they have never heard. Unless the user asks for their likes or songs they know, build at least half of the mix from those and the new_to_you catalogue tracks, with a few of their own songs mixed in. Reply with exactly one JSON object and no markdown: {\"speech\":\"spoken response, 1-3 short sentences\",\"playlist_name\":\"short name or empty string\",\"track_ids\":[\"t001\"]}. For a playlist, include 5-20 catalogue IDs in a deliberate order. If there are fewer suitable songs, use fewer. If this is only a greeting or clarifying question, playlist_name must be empty and track_ids must be []. Never invent IDs.\nCATALOGUE:\n" + JSON.stringify(catalogText);
      const all = (M.dj.messages || []).filter((m) => !m.hidden);
      const history = all.slice(all.length <= 12 ? 0 : all.length - 6 - ((all.length - 6) % 6));   // moves in steps, not every turn
      if (!hidden && history.length && history[history.length - 1].role === "user" && history[history.length - 1].content === userText) history.pop();
      const userContent = searchRows.length ? userText + "\n\nNEW SONGS FOR THIS REQUEST (found on SoundCloud just now; the user has never heard them): " + JSON.stringify(searchRows.map((x) => x.info)) : userText;
      const messages = [{ role: "system", content: system }].concat(history.map((m) => ({ role: m.role, content: m.content })), [{ role: "user", content: userContent }]);
      const response = await fetch("/api/v1/chat/completions", { method: "POST", cache: "no-store", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Nodeyard": "1" }, signal: controller.signal,
        body: JSON.stringify({ model: M.dj.target, messages, temperature: 0.65, max_tokens: 1400, stream: true }) });
      const parsed = parseDJAnswer(await readDJStream(response));
      const speech = String(parsed.speech || parsed.response || "").trim().slice(0, 2400) || "I couldn't put that into words. Try asking me a different way.";
      const byId = new Map(catalog.concat(searchRows).map((x) => [x.id, x]));
      let selectedRows = Array.isArray(parsed.track_ids) ? parsed.track_ids.slice(0, 40).map((id) => byId.get(String(id))).filter(Boolean) : [];
      selectedRows = Array.from(new Map(selectedRows.map((row) => [trackKey(row.track), row])).values());
      const selectedKeys = new Set(selectedRows.map((row) => trackKey(row.track)));
      const freshRows = catalog.filter((row) => !row.info.liked && !selectedKeys.has(trackKey(row.track)));
      if (!asksForLikes && freshRows.length && selectedRows.length > 1) {
        const targetFresh = Math.ceil(selectedRows.length / 2);
        let freshCount = selectedRows.filter((row) => !row.info.liked).length;
        for (let i = selectedRows.length - 1; i >= 0 && freshCount < targetFresh && freshRows.length; i--) {
          if (selectedRows[i].info.liked) {
            const replacement = freshRows.shift();
            selectedKeys.delete(trackKey(selectedRows[i].track));
            selectedRows[i] = replacement;
            selectedKeys.add(trackKey(replacement.track));
            freshCount++;
          }
        }
      }
      // and discovery: at least half songs the listener has never heard (three quarters when asked for something new), the
      // ones found for this request first; never for "my likes" or "songs I know"
      const newRows = searchRows.concat(catalog.filter((row) => row.info.new_to_you)).filter((row) => !selectedKeys.has(trackKey(row.track)));
      if (!wantsKnown && newRows.length && selectedRows.length > 1) {
        const targetNew = Math.ceil(selectedRows.length * (wantsNew ? 0.75 : 0.5));
        let newCount = selectedRows.filter((row) => row.info.new_to_you).length;
        // every other place from the end first (they mix in with the known songs), then any other known song but the opener
        const order = [];
        for (let i = selectedRows.length - 1; i >= 1; i -= 2) order.push(i);
        for (let i = selectedRows.length - 1; i >= 1; i--) if (!order.includes(i)) order.push(i);
        for (const i of order) {
          if (newCount >= targetNew || !newRows.length) break;
          if (!selectedRows[i].info.new_to_you) {
            const replacement = newRows.shift();
            selectedKeys.delete(trackKey(selectedRows[i].track));
            selectedRows[i] = replacement;
            selectedKeys.add(trackKey(replacement.track));
            newCount++;
          }
        }
      }
      const selected = selectedRows.map((row) => row.track);
      const name = String(parsed.playlist_name || "").trim().slice(0, 120);
      const newIn = selectedRows.filter((row) => row.info.new_to_you).length;
      const assistantText = selected.length ? speech + "\n\nI’ve queued “" + (name || "Your mix") + "” with " + selected.length + " tracks" + (newIn ? " (" + newIn + " new to you)" : "") + " and am starting playback now." : speech;
      M.dj.messages.push({ role: "assistant", content: assistantText });
      if (selected.length) {
        M.dj.draft = { name: name || "Your mix", tracks: selected, saved: false };
        playQueue(selected, 0);
      } else M.dj.draft = null;
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
    M.dj.active = true; M.dj.messages = []; M.dj.draft = null; M.dj.catalog = null; M.dj.speak = M.dj.speak !== false; saveDJ(); renderTab();
    sendDJRequest("Start my DJ session. Welcome me in one short, warm line, then ask what vibe or activity I want. Do not make a playlist yet.", true);
  }
  function stopDJ() {
    if (M.dj.controller) M.dj.controller.abort();
    M.dj.controller = null;
    stopDJVoice();
    M.dj.active = false; M.dj.busy = false; saveDJ(); renderTab();
  }

  // ---- tabs ---------------------------------------------------------------
  function renderPage() {
    const el = $("#view"); if (!el || S.view !== "music") return;
    const d = M.data || {};
    M.scLikes = Array.isArray(d.likes) ? d.likes : (Array.isArray((d.sc || {}).likes) ? d.sc.likes : []);
    M.playlists = Array.isArray(d.playlists) ? d.playlists : [];
    const tabs = [["home", "Home"], ["discover", "Discover"], ["search", "Search"], ["dj", "DJ"], ["playlists", "Playlists"], ["likes", "Likes"], ["library", "Library"], ["settings", "Settings"]];
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
    if (M.tab === "home") renderHome(el);
    else if (M.tab === "discover") renderDiscover(el);
    else if (M.tab === "search") renderSearch(el);
    else if (M.tab === "dj") renderDJ(el);
    else if (M.tab === "playlists") renderPlaylists(el);
    else if (M.tab === "likes") renderLikes(el);
    else if (M.tab === "library") renderLibrary(el);
    else if (M.tab === "settings") renderSettings(el);
  }

  function homeRecommendations() {
    const tracks = new Map();
    function add(track, source) {
      const key = trackKey(track); if (!key) return;
      let item = tracks.get(key);
      if (!item) { item = { track: { ...track, kind: track.url ? "sc" : "local" }, sources: new Set(), plays: 0, last: 0, completed: false }; tracks.set(key, item); }
      item.sources.add(source);
      if (source === "history") { item.plays++; item.last = Math.max(item.last, +track.played_at || 0); item.completed = item.completed || !!track.completed; }
    }
    M.scLikes.forEach((t) => add(t, "liked"));
    (M.homeHistory || []).forEach((t) => add(t, "history"));
    (((M.data || {}).tracks) || []).forEach((t) => add(t, "library"));
    const day = Math.floor(Date.now() / 86400000);
    function stable(key) { let h = 2166136261; for (const c of key + day) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967295; }
    return Array.from(tracks.entries()).map(([key, item]) => {
      const ageDays = item.last ? Math.max(0, (Date.now() / 1000 - item.last) / 86400) : 30;
      const score = (item.sources.has("liked") ? 8 : 0) + Math.min(item.plays, 5) * 1.5 + (item.completed ? 1 : 0) +
        (item.sources.has("library") && !item.sources.has("history") ? 3 : 0) + Math.max(0, 5 - Math.log2(ageDays + 1)) + stable(key);
      return { track: item.track, score };
    }).sort((a, b) => b.score - a.score).slice(0, 18).map((x) => x.track);
  }

  function homeUnheardRecommendations() {
    if (!M.homeHistoryReady) return [];
    const heard = new Set([...(M.homeHistory || []).map(trackKey), ...M.homeSessionHeard]);
    const candidates = new Map();
    function add(track, liked) {
      const key = trackKey(track);
      if (!key || heard.has(key)) return;
      const found = candidates.get(key);
      if (found) { found.liked = found.liked || liked; return; }
      candidates.set(key, { key, liked, track: { ...track, kind: track.url ? "sc" : "local" } });
    }
    M.scLikes.forEach((t) => add(t, true));
    (((M.data || {}).tracks) || []).forEach((t) => add(t, false));
    const day = Math.floor(Date.now() / 86400000);
    function stable(key) { let h = 2166136261; for (const c of key + day) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967295; }
    return Array.from(candidates.values()).sort((a, b) => (Number(b.liked) * 2 + stable(b.key)) - (Number(a.liked) * 2 + stable(a.key)))
      .slice(0, 18).map((x) => x.track);
  }

  function discoverRecommendations() {
    const rows = [], seen = new Set();
    const candidates = discovered().concat(M.homeHistoryReady ? homeUnheardRecommendations() : [], homeRecommendations());
    candidates.forEach((track) => { const key = trackKey(track); if (key && !seen.has(key)) { seen.add(key); rows.push(track); } });
    return rows;
  }

  function renderHome(el) {
    M.homeRecs = homeRecommendations();
    const found = discovered().filter((t) => !M.homeSessionHeard.has(trackKey(t)));
    const local = homeUnheardRecommendations();
    // new finds first, with a few unplayed songs you already own mixed in every fourth place
    const mixed = [], fq = found.slice(0, 24), lq = local.slice(0, 8);
    while (fq.length || lq.length) { for (let i = 0; i < 3 && fq.length; i++) mixed.push(fq.shift()); if (lq.length) mixed.push(lq.shift()); }
    M.homeUnheard = mixed;
    const tracks = M.homeRecs;
    const unheard = M.homeUnheard;
    const likes = M.scLikes.slice(0, 12);
    const playlists = M.playlists.slice(0, 10);
    const discStatus = M.disc.building ? html`<span class="sc-disc-status"><span class="spin small"></span> Finding new songs for you…</span>`
      : M.disc.updated ? html`<span class="sc-disc-status">Updated ${U.ago(M.disc.updated)} ago${M.disc.seeds.length ? html` · <span title="${"Started from: " + M.disc.seeds.join(", ")}">from ${M.disc.seeds.length} of your favourites and styles</span>` : ""}</span>` : "";
    const unseenContent = !M.homeHistoryReady && !unheard.length
      ? html`<div class="empty">${M.homeHistoryError ? "Couldn't check your listening history." : "Checking your listening history…"} ${M.homeHistoryError ? raw('<button class="btn small mt" data-music="home-history-retry">Try again</button>') : ""}</div>`
      : unheard.length
        ? unheard.map((t) => trackRow(t, "new:" + trackKey(t), html`${likeBtn(t)}${t.url && t.why ? html`<button class="btn small" data-music="disc-no" data-key="${t.url}" title="Not for me: show fewer like this">✕</button>` : ""}`))
        : M.disc.building || !M.disc.loaded ? html`<div class="empty"><div class="spin"></div>Looking on SoundCloud for songs that match your taste…</div>`
        : html`<div class="empty">${M.disc.error || "No new picks yet."} Like or play a few songs and press Find more.</div>`;
    setHTML(el, html`<div class="card">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h2 style="margin:0">New to you</h2>
        <div class="muted small mt">New songs found on SoundCloud from what you like and play (plus a few unplayed ones you own). It learns from likes, skips and ✕.</div>
        <div class="mt">${discStatus}</div></div>
        <div class="row" style="gap:8px">${unheard.length ? raw('<button class="btn primary" data-music="home-unheard-play">▶ Play new songs</button>') : ""}<button class="btn" data-music="disc-refresh" ${M.disc.building ? "disabled" : ""}>↻ Find more</button></div></div>
      <div class="mt">${unseenContent}</div>
    </div>
    <div class="card mt">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h2 style="margin:0">Made for you</h2>
        <div class="muted small mt">Ranked by a local algorithm using your library, likes and listening history. No AI.</div></div>
        ${tracks.length ? raw('<button class="btn primary" data-music="home-play">▶ Play recommendations</button>') : ""}</div>
      <div class="mt">${tracks.length ? tracks.map((t) => trackRow(t, "rec:" + trackKey(t), likeBtn(t)))
        : html`<div class="empty">Add songs to your library, like a few tracks, or listen to music to build your recommendations.</div>`}</div>
    </div>
    <div class="card mt">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h2 style="margin:0">Your likes <span class="faint small">(${M.scLikes.length})</span></h2>
        <div class="muted small mt">Songs you’ve saved in Nodeyard.</div></div>
        ${M.scLikes.length > likes.length ? raw('<button class="btn" data-music="home-likes">View all likes</button>') : ""}</div>
      <div class="mt">${likes.length ? likes.map((t, i) => trackRow(t, "like:" + i, likeBtn(t))) : html`<div class="empty">Like a song in Search or Discover and it will show up here.</div>`}</div>
    </div>
    <div class="card mt">
      <div class="row wrap" style="justify-content:space-between;align-items:center;gap:10px"><div><h2 style="margin:0">Your playlists <span class="faint small">(${M.playlists.length})</span></h2>
        <div class="muted small mt">Pick up a mix you’ve made.</div></div>
        ${M.playlists.length > playlists.length ? raw('<button class="btn" data-music="home-playlists">View all playlists</button>') : ""}</div>
      <div class="mt">${playlists.length ? playlists.map((p) => html`<div class="sc-row">
        <button class="sc-pbtn ghost" data-music="pl-open" data-id="${p.id}" title="Open playlist">♪</button>
        <div class="meta" data-music="pl-open" data-id="${p.id}" style="cursor:pointer"><div><b>${p.name}</b></div><div class="faint small">${p.count || 0} track${+p.count === 1 ? "" : "s"}</div></div>
        <button class="btn small" data-music="pl-open" data-id="${p.id}">Open</button></div>`)
        : html`<div class="empty">Create a playlist from Search, Likes, Library or the DJ.</div>`}</div>
    </div>`);
  }

  function artworkSource(t) {
    if (t && t.artwork_url) return "/api/music/art?url=" + encodeURIComponent(t.artwork_url);
    if (t && t.path) return "/api/music/art?path=" + encodeURIComponent(t.path);
    return "";
  }

  function renderDiscover(el) {
    if (!M.discoverSeeded) { M.discoverTracks = discoverRecommendations(); M.discoverIndex = 0; M.discoverSeeded = true; }
    const tracks = M.discoverTracks || [];
    const track = tracks[M.discoverIndex];
    if (!track) {
      setHTML(el, html`<div class="card"><h2 style="margin-top:0">Discover · For you</h2>
        <p class="muted">Your feed uses saved songs from your library, listening history and likes. It doesn’t need a search.</p>
        <div class="empty">Add music to your library or like a song to seed your recommendations.</div>
        <div class="row wrap mt"><button class="btn" data-music="discover-library">Open Library</button><button class="btn" data-music="discover-likes">Open Likes</button></div>
      </div>`);
      return;
    }
    const art = artworkSource(track);
    const background = art ? "background-image:url('" + art + "')" : "";
    const liked = M.scLikes.some((item) => trackKey(item) === trackKey(track));
    setHTML(el, html`<div class="sc-discover-feed"><header class="sc-discover-feed-head"><div><h2>Discover</h2><span>For you · new finds first, then your music</span></div><b>${M.discoverIndex + 1} / ${tracks.length}${M.discoverMoreBusy ? raw(' <span class="spin small" title="Finding more songs"></span>') : ""}</b></header>
      <article class="sc-discover-card" data-discover-stage style="${background}" tabindex="0" aria-label="Discover song: ${titleOf(track)}. Swipe up or down to browse.">
        <div class="sc-discover-shade"></div>
        <div class="sc-discover-cover">${art ? html`<img src="${art}" alt="${titleOf(track)} artwork" loading="lazy">` : html`<span aria-hidden="true">♫</span>`}</div>
        <aside class="sc-discover-actions">
          <button type="button" data-music="discover-like" aria-label="${liked ? "Unlike" : "Like"} song">${liked ? "♥" : "♡"}</button><small>${liked ? "Liked" : "Like"}</small>
          <button type="button" data-music="discover-play" aria-label="Play song">▶</button><small>Play</small>
          <button type="button" data-music="discover-add" aria-label="Add song to playlist">＋</button><small>Save</small>
        </aside>
        <div class="sc-discover-meta"><span class="chip">${track.why ? "New for you" : track.url ? "SoundCloud" : "Your library"}</span><h2>${titleOf(track)}</h2>
          <div>${subOf(track)}${track.duration_ms ? " · " + fmtTime(track.duration_ms / 1000) : ""}</div>${track.why ? html`<div class="sc-why">${track.why}</div>` : ""}</div>
        <div class="sc-discover-nav"><button type="button" data-music="discover-prev" ${M.discoverIndex <= 0 ? "disabled" : ""} aria-label="Previous song">⌃</button><button type="button" data-music="discover-next" ${M.discoverIndex >= tracks.length - 1 && !M.discoverMoreBusy ? "disabled" : ""} aria-label="Next song">⌄</button></div>
        <div class="sc-discover-hint">Swipe or scroll · ↑ / ↓ · each song plays as you land on it</div>
      </article></div>`);
    warmDiscover();
    if (tracks.length - M.discoverIndex <= 8) moreDiscover();
  }
  // the next two songs in the feed load in the background, one at a time (the server keeps them ready), and their
  // covers too, so landing on them plays and shows at once
  const discoverWarm = new Set(), discoverWarmTried = new Map();
  let discoverWarming = false;
  async function warmDiscover() {
    if (discoverWarming) return;
    discoverWarming = true;
    try {
      for (const ahead of [1, 2]) {
        const t = M.discoverTracks[M.discoverIndex + ahead];
        if (!t) break;
        const art = artworkSource(t);
        if (art && !discoverWarm.has("art:" + art)) { discoverWarm.add("art:" + art); new Image().src = art; }
        if (!(t.kind === "sc" || t.url)) continue;          // (library files start instantly anyway)
        const key = trackKey(t);
        if (discoverWarm.has(key) || Date.now() < (discoverWarmTried.get(key) || 0)) continue;
        try {
          const r = await fetch(srcFor(t), { headers: { Range: "bytes=0-1" }, cache: "no-store", credentials: "same-origin" });
          if (r.ok) discoverWarm.add(key);
          else discoverWarmTried.set(key, Date.now() + 60000);   // (busy or can't play: not asked again for a minute)
        } catch (e) { discoverWarmTried.set(key, Date.now() + 60000); }
      }
      if (discoverWarm.size > 300) { discoverWarm.clear(); discoverWarmTried.clear(); }
    } finally { discoverWarming = false; }
  }
  // an endless feed: with 8 songs left, more are fetched from what's on screen (SoundCloud's "plays next" for the
  // song you're on, else a search from your taste), never ones you've heard or already in the feed
  async function moreDiscover() {
    if (M.discoverMoreBusy || Date.now() < (M.discoverMoreRetry || 0)) return;
    const feed = M.discoverTracks, cur = feed[M.discoverIndex] || {};
    const sc = feed.filter((t) => t.url);
    const seeds = [cur.url, sc.length ? sc[sc.length - 1].url : ""].filter(Boolean);
    M.discoverMoreBusy = true; renderTab();
    let added = 0;
    try {
      const r = await postJSON("/api/music/discover-more", { seeds, exclude: sc.map((t) => t.url).slice(-1500) });
      const have = new Set(M.discoverTracks.map(trackKey));
      (r.tracks || []).forEach((t) => { const k = trackKey(t); if (k && !have.has(k)) { have.add(k); M.discoverTracks.push(t); added++; } });
    } catch (e) { /* tried again on the next move */ }
    M.discoverMoreBusy = false;
    if (!added) M.discoverMoreRetry = Date.now() + 30000;       // (nothing new this time: don't ask on every move)
    if (S.view === "music" && M.tab === "discover") {
      renderTab();
      if (added && M.discoverWantNext) { M.discoverWantNext = false; moveDiscover(1); }
    }
  }
  let discoverPlayTimer = 0;
  function moveDiscover(delta) {
    if (!M.discoverTracks.length) return;
    const before = M.discoverIndex;
    M.discoverIndex = Math.max(0, Math.min(M.discoverTracks.length - 1, M.discoverIndex + delta));
    if (M.discoverTracks.length - M.discoverIndex <= 8) moreDiscover();
    if (M.discoverIndex === before && delta > 0) M.discoverWantNext = M.discoverMoreBusy;    // at the end: carry on when more arrive
    renderTab();
    if (M.discoverIndex === before) return;
    // scrolling onto a song plays it: after a moment, so flicking past several doesn't start (and fetch) each one
    clearTimeout(discoverPlayTimer);
    discoverPlayTimer = setTimeout(() => {
      const t = M.discoverTracks[M.discoverIndex];
      if (!t || S.view !== "music" || M.tab !== "discover") return;
      const cur = M.queue[M.idx];
      if (cur && !M.live && trackKey(cur) === trackKey(t) && audio && !audio.paused) return;   // (already playing)
      playQueue(M.discoverTracks, M.discoverIndex);
    }, 350);
  }

  function renderSearch(el) {
    const sc = (M.data || {}).sc || {};
    const playlistResults = M.playlistResults.length
      ? html`<div class="section-title">Your playlists</div>${M.playlistResults.map((p) => html`<div class="sc-row">
          <button class="sc-pbtn ghost" data-music="pl-open" data-id="${p.id}" title="Open playlist">♪</button>
          <div class="meta" data-music="pl-open" data-id="${p.id}" style="cursor:pointer"><div><b>${p.name}</b></div><div class="faint small">${p.count} track${p.count === 1 ? "" : "s"}</div></div>
          <button class="btn small" data-music="pl-open" data-id="${p.id}">Open</button></div>`)}`
      : "";
    const setBody = M.scSet
      ? html`<div class="row" style="justify-content:space-between;align-items:center;margin-top:4px">
          <b>${M.scSet.title} <span class="faint small">(${M.scSet.tracks.length})</span></b>
          <button class="btn small primary" data-music="play-set">▶ Play all</button></div>
        ${M.scSet.tracks.map((t) => trackRow(t, "set:" + trackKey(t), likeBtn(t)))}`
      : "";
    const results = M.scResults.length
      ? html`<div class="section-title">Results</div>${M.scResults.map((t) => trackRow(t, "res:" + trackKey(t), likeBtn(t)))}`
      : "";
    setHTML(el, html`
      <div class="card">
        <div class="row" style="gap:8px"><input class="input" data-music="sc-query" value="${M.searchQuery}" placeholder="Search songs, artists and playlists…" spellcheck="false" style="flex:1">
          <button class="btn primary" data-music="sc-search">Search</button></div>
        <label class="check mt" style="font-size:13px"><input type="checkbox" data-music="nodrm" ${M.hideDrm ? raw("checked") : ""}> <span>Only show playable tracks — hide DRM-protected (Go+) ones <span class="faint">(slower; checks each result)</span></span></label>
        ${!sc.search_ready ? html`<p class="small mt">SoundCloud search needs <span class="mono">yt-dlp</span> on the server. You can still search your saved playlists. Use <b>Settings → Update everything</b> to install SoundCloud search.</p>` : ""}
        ${sc.search_ready && !sc.stream_ready ? html`<p class="small faint mt">In-browser playback also needs ffmpeg — run <b>Settings → Update everything</b>. Until then use ＋ to save tracks and ↗ to open on SoundCloud.</p>` : ""}
        <div id="sc-results" class="mt">${M.searchError ? html`<p class="note warn small">${M.searchError}</p>` : ""}${playlistResults}${results}${M._searched && !M.playlistResults.length && !M.scResults.length && !M.searchError ? html`<div class="empty">No ${M.hideDrm ? "playable " : ""}results${M.hideDrm ? " — all matches were DRM-protected" : ""}.</div>` : ""}</div>
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
    const key = trackKey(t);
    const liked = M.scLikes.some((x) => trackKey(x) === key);
    return html`<button class="btn small" data-music="sc-like" data-key="${key}" title="${liked ? "Unlike" : "Like"}" aria-label="${liked ? "Unlike" : "Like"} ${titleOf(t)}">${liked ? "♥" : "♡"}</button>
      ${t.url ? html`<button class="btn small" data-music="sc-dl" data-key="${t.url}" title="Download to Nodeyard Music">⬇</button>` : ""}`;
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
    const CAP = 300;                         // only build this many rows; big libraries kill the DOM otherwise
    const rows = shown.slice(0, CAP);
    const uploads = tracks.filter((t) => (t.folder || "") === "Uploads" || (t.folder || "").indexOf("Uploads") === 0);
    setHTML(el, html`<div class="card music-library-drop" data-music-library-drop>
      <div class="row" style="justify-content:space-between;align-items:center;gap:8px">
        <input class="input" data-music="lib-filter" value="${M._libq || ""}" placeholder="Filter the library…" style="flex:1">
        <button class="btn small" data-music="lib-upload">⬆ Upload songs</button>
        ${shown.length ? raw('<button class="btn small primary" data-music="lib-play">▶ Play all</button>') : ""}</div>
      ${raw('<input type="file" data-music="lib-file" accept="audio/*,.mp3,.m4a,.flac,.ogg,.opus,.wav,.aac" multiple style="display:none">')}
      <div data-music="up-progress" class="small faint mt" style="${M.uploadMessage ? "" : "display:none"}" role="status" aria-live="polite">${M.uploadMessage}</div>
      <div class="small faint mt">Choose files above or drag audio files into this panel.</div>
      ${uploads.length ? html`<div class="mt"><div class="small faint">Your uploads (${uploads.length}) — tap ✕ to remove</div>
        <div class="row wrap mt" style="gap:6px">${uploads.map((t) => html`<span class="tag" style="display:inline-flex;align-items:center;gap:6px">${t.name}
          ${raw('<button class="btn small" data-music="up-del" data-path="' + esc(t.path) + '" title="Delete" style="padding:0 6px;line-height:1.4">✕</button>')}</span>`)}</div></div>` : ""}
      ${!(M.data || {}).dir && !uploads.length ? html`<p class="small mt">No music folder set — pick one in <b>Settings</b>, or upload songs above.</p>` : ""}
      <div class="mt">${shown.length
        ? html`${rows.map((t) => trackRow(t, "lib:" + trackKey(t), likeBtn(t)))}${shown.length > CAP
            ? html`<div class="small faint mt" style="text-align:center">Showing ${CAP} of ${shown.length} — type in the filter to narrow, or press Play all to queue them all.</div>` : ""}`
        : html`<div class="empty">${(M.data || {}).dir || uploads.length ? "No matching files." : "Upload songs above, or set a music folder in Settings."}</div>`}</div></div>`);
  }

  async function uploadSong(file, onProgress) {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const id = (window.crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : (Date.now().toString(16) + Math.random().toString(16).slice(2))).slice(0, 12);
    const CH = 2 * 1024 * 1024;   // raw bytes per piece (server piece limit is 3 MB)
    let offset = 0; const total = file.size;
    if (!total) throw new Error(file.name + " is empty.");
    if (total > 80 * 1024 * 1024) throw new Error(file.name + " is too big (80 MB at most).");
    try {
      do {
        const slice = file.slice(offset, Math.min(offset + CH, total));
        const buf = new Uint8Array(await slice.arrayBuffer());
        let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        const done = offset + slice.size >= total;
        const r = await postJSON("/api/music/upload", { id, ext, name: file.name, offset, data: btoa(bin), done });
        if (!r || !r.ok) throw new Error((r && r.error) || "Upload failed.");
        offset += slice.size;
        if (onProgress) onProgress(offset, total);
      } while (offset < total);
    } catch (e) {
      // A failed later chunk must not leave an orphaned .part file on the server.
      try { await postJSON("/api/music/upload-cancel", { id }); } catch (cleanupError) { /* stale parts are also pruned on the next upload */ }
      throw e;
    }
  }

  async function uploadFiles(files) {
    const list = Array.from(files || []); if (!list.length) return;
    M.uploadMessage = "Preparing uploads…";
    const failed = [], progress = list.map(() => 0), active = new Map();
    const totalBytes = list.reduce((sum, f) => sum + f.size, 0);
    let done = 0, next = 0;
    const paint = () => {
      const current = Array.from(active.values());
      const sent = progress.reduce((sum, bytes) => sum + bytes, 0);
      const percent = totalBytes ? Math.min(100, Math.floor(sent / totalBytes * 100)) : 0;
      const status = current.length ? "Uploading " + current.join(", ") : "Preparing uploads";
      const message = status + " — " + percent + "% overall · " + done + "/" + list.length + " complete";
      const node = $('[data-music="up-progress"]');
      M.uploadMessage = message;
      if (node) { node.style.display = ""; node.textContent = message; }
    };
    paint();
    async function worker() {
      while (next < list.length) {
        const i = next++; const file = list[i]; active.set(i, file.name); paint();
        try {
          await uploadSong(file, (sent) => { progress[i] = sent; paint(); });
          progress[i] = file.size; done++;
        } catch (e) {
          progress[i] = 0; failed.push({ name: file.name, error: e && e.message || "Upload failed." });
        } finally { active.delete(i); paint(); }
      }
    }
    // Two workers overlap network waits while keeping browser memory and server write pressure bounded.
    await Promise.all(Array.from({ length: Math.min(2, list.length) }, () => worker()));
    const failedNames = failed.length ? " Failed: " + failed.slice(0, 3).map((item) => item.name).join(", ") + (failed.length > 3 ? "…" : "") : "";
    const summary = (done
      ? "Uploaded " + done + " of " + list.length + " song" + (list.length === 1 ? "" : "s") + (failed.length ? "; " + failed.length + " failed." : ".")
      : "Nothing was uploaded. " + failed.length + " file" + (failed.length === 1 ? "" : "s") + " failed.") + failedNames;
    M.uploadMessage = summary;
    const node = $('[data-music="up-progress"]'); if (node) { node.style.display = ""; node.textContent = summary; }
    await load(); renderTab();
    if (done) toast(summary);
    else if (failed.length) toast(failed[0].error || summary);
  }

  function renderSettings(el) {
    const d = M.data || {}; const fw = d.forward || {};
    const cache = d.recent_cache || { limit: 20, count: 0, folder: "" };
    const cacheOptions = [10, 20, 30, 50, 100, 200].map((n) => '<option value="' + n + '"' + (+cache.limit === n ? " selected" : "") + '>' + n + ' songs</option>').join("");
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
      ${card("Music folder", html`<p class="muted small" style="margin-top:0">The server plays audio files from one folder on the host (the control node, not your Mac). Everyone signed in can listen. Downloads from this page go into the private Downloads folder in your library.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="dir" value="${d.dir || ""}" placeholder="/home/your-user/Music/soundcloud" spellcheck="false" style="flex:1">
          <button class="btn" data-music="browse">Browse…</button><button class="btn primary" data-music="save-dir">Save</button></div>`)}
      ${card("Recently played cache", html`<p class="muted small" style="margin-top:0">Played SoundCloud songs stay in a private cache folder so they load faster next time. When full, the oldest song is removed.</p>
        <div class="row wrap" style="gap:10px;align-items:center"><label class="small">Keep up to <select class="select" data-music="cache-limit">${raw(cacheOptions)}</select></label>
          <span class="faint small">${cache.count} cached · ${cache.folder || "music cache"}</span></div>`)}
      ${card("Live audio forwarding", fwBody)}`);
  }

  // ---- add-to-playlist picker --------------------------------------------
  function findTrack(key) {
    return [M.queue, M.scResults, M.discoverTracks, M.disc.tracks, M.scLikes, (M.scSet && M.scSet.tracks) || [], (M.openPl && M.openPl.tracks) || [],
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
      (M.playlists.length > 4 ? '<input class="input" data-music="pk-filter" placeholder="Filter playlists…" aria-label="Filter playlists" style="margin-bottom:6px">' : "") +
      '<div style="overflow:auto;flex:1" data-pk-list>' +
      (M.playlists.length ? M.playlists.map((p) => '<button class="btn small" data-music="pk-add" data-id="' + esc(p.id) + '" style="display:block;width:100%;text-align:left;margin:3px 0">♪ ' + esc(p.name) + ' <span class="faint small">(' + p.count + ")</span></button>").join("") : '<div class="faint small">No playlists yet — make one above.</div>') +
      '</div><div class="faint small" data-pk-empty hidden>No matching playlists.</div></div>';
  }
  const closeAddPicker = () => { const m = document.getElementById("pl-picker"); if (m) m.remove(); M.addTrack = null; };
  async function addToPlaylist(pid, track) {
    const r = await postJSON("/api/music/playlist", { action: "add", id: pid, track: trackForApi(track) });
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

  async function refreshHomeHistory() {
    M.homeHistoryReady = false; M.homeHistoryError = false;
    try {
      const first = await getJSON("/api/music/ai?action=history&offset=0&limit=50");
      if (!first.ok || !Array.isArray(first.tracks)) throw new Error(first.error || "History is unavailable.");
      const total = Math.max(first.tracks.length, Math.min(500, +first.total || 0));
      const offsets = [];
      for (let offset = 50; offset < total; offset += 50) offsets.push(offset);
      const pages = await Promise.all(offsets.map((offset) => getJSON("/api/music/ai?action=history&offset=" + offset + "&limit=50")));
      if (pages.some((page) => !page.ok || !Array.isArray(page.tracks))) throw new Error("Some listening history pages could not be loaded.");
      M.homeHistory = first.tracks.concat(...pages.map((page) => page.tracks));
      M.homeHistoryReady = true;
    } catch (e) {
      M.homeHistory = []; M.homeHistoryError = true;
    }
  }

  async function load() {
    try { const r = await getJSON("/api/music"); if (r.ok) M.data = r; } catch (e) { /* signed out */ }
    loadDiscover(false);
    await refreshHomeHistory();
  }

  // ---- new songs for you: the server finds them (from your likes and listening) and keeps them fresh ------------
  async function loadDiscover(refresh) {
    clearTimeout(M.disc.timer);
    try {
      const r = await getJSON("/api/music/discover" + (refresh ? "?refresh=1" : ""));
      if (r.ok) Object.assign(M.disc, { tracks: r.tracks || [], updated: r.updated || 0, building: !!r.building, error: r.error || "", seeds: r.seeds || [], loaded: true });
    } catch (e) { M.disc.loaded = true; }
    if (M.disc.building && M.disc.polls++ < 60) M.disc.timer = setTimeout(() => loadDiscover(false), 4000);
    else M.disc.polls = 0;
    if (S.view === "music" && (M.tab === "home" || M.tab === "discover")) {
      if (M.tab === "discover" && !M.disc.building) M.discoverSeeded = false;
      renderTab();
    }
  }
  const discovered = () => (M.disc.tracks || []).map((t) => ({ ...t, kind: "sc" }));

  async function searchSc() {
    const input = $('[data-music="sc-query"]');
    const query = (input && input.value || "").trim();
    if (query.length < 2) { toast("Enter at least two characters to search."); return; }
    M.searchQuery = query;
    const lower = query.toLocaleLowerCase();
    M.playlistResults = M.playlists.filter((p) => String(p.name || "").toLocaleLowerCase().includes(lower));
    M.searchError = "";
    const el = $("#sc-results"); if (el) setHTML(el, html`<div class="empty"><div class="spin"></div>${M.hideDrm ? "Searching and checking which tracks are playable…" : "Searching…"}</div>`);
    try {
      const r = await getJSON("/api/music/search?q=" + encodeURIComponent(query) + (M.hideDrm ? "&nodrm=1" : ""));
      M._searched = true;
      if (!r.ok) { M.scResults = []; M.searchError = r.error || "SoundCloud search failed."; renderTab(); return; }
      M.scResults = Array.isArray(r.tracks) ? r.tracks : [];
      renderTab();
    } catch (e) { M._searched = true; M.scResults = []; M.searchError = e.message || "SoundCloud search failed."; renderTab(); }
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
    if (t.dataset.tab) {
      M.tab = t.dataset.tab; M.openPl = null;
      if (M.tab === "dj") loadDJTargets(!((M.dj.targets || {}).targets || []).some((x) => x.ready));
      if (M.tab === "discover") M.discoverSeeded = false;
      renderPage();
      if (M.tab === "home" || M.tab === "discover") { const requestedTab = M.tab; const pending = refreshHomeHistory(); renderTab(); pending.then(() => { if (S.view === "music" && M.tab === requestedTab) { if (requestedTab === "discover") M.discoverSeeded = false; renderTab(); } }); }
      return;
    }
    const act = t.dataset.music;
    if (act === "listen") playLive();
    else if (act === "home-play") { if (M.homeRecs.length) playQueue(M.homeRecs, 0); }
    else if (act === "home-unheard-play") { if (M.homeUnheard.length) playQueue(M.homeUnheard, 0); }
    else if (act === "disc-refresh") { M.disc.building = true; renderTab(); loadDiscover(true); }
    else if (act === "disc-no") {
      const track = findTrack(t.dataset.key); if (!track) return;
      M.disc.tracks = M.disc.tracks.filter((x) => x.url !== track.url); renderTab();
      const r = await postJSON("/api/music/discover", { action: "dislike", track });
      if (!r.ok) toast(r.error || "Couldn't save that."); else { toast("Got it — fewer like that."); Object.assign(M.disc, { tracks: r.tracks || M.disc.tracks, building: !!r.building }); if (r.building) loadDiscover(false); else renderTab(); }
    }
    else if (act === "home-history-retry") { const pending = refreshHomeHistory(); renderTab(); pending.then(() => { if (S.view === "music" && M.tab === "home") renderTab(); }); }
    else if (act === "home-likes") { M.tab = "likes"; renderPage(); }
    else if (act === "home-playlists") { M.tab = "playlists"; renderPage(); }
    else if (act === "dj-start") startDJ();
    else if (act === "dj-stop") stopDJ();
    else if (act === "dj-preview-voice") previewDJVoice();
    else if (act === "dj-get-voice") {
      try { const r = await postJSON("/api/music/dj-voice/setup", { voice: piperVoice() }); if (r.ok) { M.dj.piper = r; renderTab(); setTimeout(() => loadPiper(true), 1500); } else toast(r.error || "Couldn't start the download."); }
      catch (e) { toast("Couldn't reach the dashboard server."); }
    }
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
    else if (act === "lib-upload") { const inp = $('[data-music="lib-file"]'); if (inp) inp.click(); }
    else if (act === "up-del") { if (confirm("Delete this uploaded song?")) { const r = await postJSON("/api/music/upload-delete", { path: t.dataset.path }); if (r.ok) { await load(); renderTab(); toast("Deleted."); } else toast(r.error || "Couldn't delete it."); } }
    else if (act === "save-dir") await saveDir($('[data-music="dir"]').value.trim());
    else if (act === "browse") openBrowse($('[data-music="dir"]').value.trim());
    else if (act === "b-open" || act === "b-short") openBrowse(t.dataset.path);
    else if (act === "b-cancel") closeBrowse();
    else if (act === "b-use") { closeBrowse(); await saveDir(t.dataset.path); }
    else if (act === "sc-search") await searchSc();
    else if (act === "discover-prev") moveDiscover(-1);
    else if (act === "discover-next") moveDiscover(1);
    else if (act === "discover-play") { if (M.discoverTracks[M.discoverIndex]) playQueue(M.discoverTracks, M.discoverIndex); }
    else if (act === "discover-like") { const tr = M.discoverTracks[M.discoverIndex]; if (tr) await toggleLike(trackKey(tr)); }
    else if (act === "discover-add") { const tr = M.discoverTracks[M.discoverIndex]; if (tr) openAddPicker(tr); }
    else if (act === "discover-library") { M.tab = "library"; renderPage(); }
    else if (act === "discover-likes") { M.tab = "likes"; renderPage(); }
    else if (act === "play-url") await playTrackLink();
    else if (act === "load-set") await loadSet();
    else if (act === "play-set") { if (M.scSet) playQueue(M.scSet.tracks, 0); }
    else if (act === "likes-play") playQueue(M.scLikes, 0);
    else if (act === "lib-play") playQueue(filteredLib(), 0);
    else if (act === "sc-like") await toggleLike(t.dataset.key);
    else if (act === "sc-dl") {
      toast("Downloading to the music folder…");
      const r = await postJSON("/api/music/sc-download", { url: t.dataset.key });
      toast(r.ok ? (r.text || "Saved to Nodeyard Music.") : (r.error || "Download failed."));
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
    else if (kind === "rec") { const i = M.homeRecs.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(M.homeRecs, i); }
    else if (kind === "new") { const i = M.homeUnheard.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(M.homeUnheard, i); }
  }
  async function toggleLike(url) {
    const track = findTrack(url); if (!track) return;
    const liked = !M.scLikes.some((x) => trackKey(x) === url);
    const r = await postJSON("/api/music/like", { track: trackForApi(track), liked });
    if (!r.ok) { toast(r.error || "Couldn't save that like."); return; }
    M.scLikes = Array.isArray(r.likes) ? r.likes : [];
    if (M.data) M.data.likes = M.scLikes;
    if (M.data && M.data.sc) M.data.sc.likes = M.scLikes.filter((x) => x.url);   // SoundCloud consumers use this list
    renderTab();
  }
  async function openPlaylist(id) {
    const r = await getJSON("/api/music/playlists?id=" + encodeURIComponent(id));
    if (r.ok && r.playlist) { M.openPl = r.playlist; renderTab(); } else toast("Couldn't open that playlist.");
  }

  document.addEventListener("input", (e) => {
    if (e.target.matches('[data-music="lib-filter"]')) { M._libq = e.target.value; renderTab(); const inp = $('[data-music="lib-filter"]'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } }
    else if (e.target.matches('[data-music="pk-filter"]')) {
      const q = e.target.value.trim().toLocaleLowerCase(); let shown = 0;
      document.querySelectorAll("#pl-picker [data-music=pk-add]").forEach((button) => {
        const match = button.textContent.toLocaleLowerCase().includes(q); button.hidden = !match; if (match) shown++;
      });
      const empty = document.querySelector("#pl-picker [data-pk-empty]"); if (empty) empty.hidden = shown > 0;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (S.view === "music" && M.tab === "discover" && (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) &&
        !e.target.matches("input,textarea,select,[contenteditable=true]")) {
      e.preventDefault(); moveDiscover(e.key === "ArrowUp" || e.key === "ArrowLeft" ? -1 : 1); return;
    }
    if ((S.view === "music" || S.view === "desk") && !(e.target.matches && e.target.matches("input,textarea,select,[contenteditable=true]")) && !e.ctrlKey && !e.metaKey && !e.altKey && audio && (M.queue[M.idx] || M.live)) {
      if (e.key === "m" || e.key === "M") { e.preventDefault(); toggleMute(); return; }
      if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !(S.view === "music" && M.tab === "discover")) {
        e.preventDefault();
        if (e.shiftKey) { if (e.key === "ArrowLeft") { if (M.idx > 0 && !M.live) play(M.idx - 1); } else if (!M.live) next(); }
        else NY.music.skip(e.key === "ArrowLeft" ? -10 : 10);
        return;
      }
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.matches('[data-music="dj-request"]')) { e.preventDefault(); $('[data-music="dj-send"]')?.click(); return; }
    if (e.key !== "Enter") return;
    if (e.target.matches('[data-music="sc-query"]')) { e.preventDefault(); searchSc(); }
    else if (e.target.matches('[data-music="track-url"]')) { e.preventDefault(); playTrackLink(); }
    else if (e.target.matches('[data-music="set-url"]')) { e.preventDefault(); loadSet(); }
  });
  document.addEventListener("pointerdown", (e) => {
    if (M.tab === "discover" && e.target.closest("[data-discover-stage]")) M.discoverPointerY = e.clientY;
  });
  document.addEventListener("pointerup", (e) => {
    if (M.discoverPointerY === null) return;
    const delta = e.clientY - M.discoverPointerY; M.discoverPointerY = null;
    if (Math.abs(delta) > 55 && !e.target.closest("button")) moveDiscover(delta < 0 ? 1 : -1);
  });
  document.addEventListener("wheel", (e) => {
    if (M.tab !== "discover" || !e.target.closest("[data-discover-stage]") || Math.abs(e.deltaY) < 25 || Date.now() - M.discoverWheelAt < 450) return;
    e.preventDefault(); M.discoverWheelAt = Date.now(); moveDiscover(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });
  const audioExtensions = new Set(["mp3", "m4a", "flac", "ogg", "opus", "wav", "aac"]);
  const libraryDropTarget = (e) => e.target && e.target.closest && e.target.closest("[data-music-library-drop]");
  const isFileDrag = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  document.addEventListener("dragenter", (e) => {
    const card = libraryDropTarget(e); if (!card || !isFileDrag(e)) return;
    e.preventDefault(); card.classList.add("dragover");
  });
  document.addEventListener("dragover", (e) => {
    const card = libraryDropTarget(e); if (!card || !isFileDrag(e)) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy"; card.classList.add("dragover");
  });
  document.addEventListener("dragleave", (e) => {
    const card = libraryDropTarget(e);
    if (card && (!e.relatedTarget || !card.contains(e.relatedTarget))) card.classList.remove("dragover");
  });
  document.addEventListener("drop", async (e) => {
    const card = libraryDropTarget(e); if (!card || !isFileDrag(e)) return;
    e.preventDefault(); card.classList.remove("dragover");
    const files = Array.from(e.dataTransfer.files || []);
    const audioFiles = files.filter((f) => (f.type || "").toLowerCase().startsWith("audio/") || audioExtensions.has((f.name.split(".").pop() || "").toLowerCase()));
    if (audioFiles.length < files.length) toast("Skipped " + (files.length - audioFiles.length) + " non-audio file" + (files.length - audioFiles.length === 1 ? "" : "s") + ".");
    if (audioFiles.length) await uploadFiles(audioFiles);
    else if (files.length) toast("Drop audio files such as MP3, M4A, FLAC, OGG, WAV or AAC.");
  });
  document.addEventListener("change", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    if (t.dataset.music === "cache-limit") { const r = await postJSON("/api/music/config", { cache_limit: +t.value }); if (r.ok) { await load(); renderTab(); toast("Recent song cache updated."); } else toast(r.error || "Couldn't update the cache size."); }
    else if (t.dataset.music === "dj-target") { M.dj.target = t.value; saveDJ(); }
    else if (t.dataset.music === "dj-voice") { M.dj.voice = t.value; saveDJ(); renderTab(); }
    else if (t.dataset.music === "dj-between") { M.dj.between = t.checked; saveDJ(); }
    else if (t.dataset.music === "dj-speak") { M.dj.speak = t.checked; saveDJ(); if (!t.checked) stopDJVoice(); }
    else if (t.dataset.music === "fw-toggle") { const r = await postJSON("/api/music/config", { forward_enabled: t.checked }); if (r.ok) { await load(); renderTab(); } }
    else if (t.dataset.music === "fw-source") { await postJSON("/api/music/config", { forward_source: t.value }); await load(); renderTab(); }
    else if (t.dataset.music === "lib-file") {
      // Snapshot before clearing the input: clearing it can empty its FileList, and reset lets you pick the same song again.
      const files = Array.from(t.files || []);
      t.value = "";
      if (files.length) await uploadFiles(files);
    }
    else if (t.dataset.music === "nodrm") { M.hideDrm = t.checked; try { localStorage.setItem("ny-hidedrm", t.checked ? "1" : "0"); } catch (e) { /* ignore */ } }
  });

  // ---- the same player, for other pages (the Desk) ------------------------
  // Pages listen for the "ny-music" document event and read NY.music.state(); they never get their own <audio>.
  function notify() { document.dispatchEvent(new CustomEvent("ny-music")); }
  const describe = (t) => ({ key: trackKey(t), title: titleOf(t), sub: subOf(t), art: artworkSource(t) });
  NY.music = {
    describe,
    state() {
      const t = M.live ? null : M.queue[M.idx];
      const known = t && t.duration_ms ? t.duration_ms / 1000 : 0;
      return { live: M.live, track: t ? describe(t) : null, playing: !!(audio && !audio.paused && audio.src),
        current: audio ? audio.currentTime || 0 : 0, duration: known || (audio && isFinite(audio.duration) ? audio.duration : 0),
        seekable: !!(t && M.seekable), hasPrev: !!t && M.idx > 0, hasNext: !!t && M.queue.length > 0, shuffle: M.shuffle, repeat: M.repeat,
        volume: audio ? audio.volume : 1, muted: !!(audio && audio.muted), sleep: sleepLabel() };
    },
    toggle() {
      if (!audio || (!M.live && !M.queue[M.idx])) return false;
      if (audio.paused) audio.play().catch(() => toast("Playback was blocked. Press Play again."));
      else audio.pause();
      return true;
    },
    next() { if (audio && !M.live && M.queue[M.idx]) next(); },
    prev() { if (audio && !M.live && M.idx > 0) play(M.idx - 1); },
    seek(fraction) { if (audio && !M.live && M.seekable && audio.duration) audio.currentTime = Math.max(0, Math.min(1, +fraction || 0)) * audio.duration; },
    setShuffle(on) { ensureBar(); M.shuffle = !!on; resetShuffle(M.idx); paintBtn(); savePlayback(false, true); },
    playQueue(tracks, i) { ensureBar(); playQueue(tracks, i); },
    setVolume, toggleMute: () => { ensureBar(); toggleMute(); }, cycleSleep: () => { ensureBar(); cycleSleep(); },
    skip(seconds) { if (audio && !M.live && M.seekable && audio.duration) audio.currentTime = Math.max(0, Math.min(audio.duration - 0.5, audio.currentTime + seconds)); },
    restore() { return restorePlayback(); },
    resume() { ensureBar(); return restorePlayback(true); },
    cycleRepeat,
  };

  V.music = {
    build() { ensureBar(); NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(renderPage); restorePlayback(); },
    update() { },
  };
  if (typeof speechSynthesis !== "undefined") speechSynthesis.addEventListener("voiceschanged", () => {
    const select = $('[data-music="dj-voice"]'); if (select) select.innerHTML = djVoiceOptions();
  });
})();

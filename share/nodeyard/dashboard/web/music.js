// The Music page — a small SoundCloud-style player — and the persistent player bar.
// The server streams a folder of music (with seeking), streams SoundCloud tracks as MP3 (so they play in the browser),
// and, when the admin turns it on, forwards the host's own audio output as live MP3. A browser only needs the dashboard.
// Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, toast, getJSON, postJSON } = U;

  const M = {
    data: null, tab: "search",
    queue: [], idx: -1, live: false, seekable: true, shuffle: false, repeat: false,
    scResults: [], scSet: null, scLikes: [], playlists: [], openPl: null, addTrack: null,
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
      "#ny-player button.act{color:var(--sc,#ff5500)}" +
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
      '<button id="ny-repeat" title="Repeat">🔁</button>' +
      '<button id="ny-vol-i" title="Volume">🔊</button><input type="range" id="ny-vol" min="0" max="1" value="1" step="0.05" style="width:70px" aria-label="Volume">' +
      '<button id="ny-close" title="Close player">✕</button>';
    document.body.appendChild(bar);
    audio = new Audio();
    audio.preload = "metadata";
    audio.crossOrigin = "anonymous";
    audio.addEventListener("timeupdate", tick);
    audio.addEventListener("durationchange", tick);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("play", () => { startViz(); paintBtn(); });
    audio.addEventListener("pause", paintBtn);
    audio.addEventListener("error", () => { if (!M.live) toast("Couldn't play that track."); });
    $("#ny-play").onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
    $("#ny-prev").onclick = () => { if (!M.live) play(M.idx - 1); };
    $("#ny-next").onclick = () => { if (!M.live) next(); };
    $("#ny-close").onclick = stop;
    $("#ny-shuffle").onclick = () => { M.shuffle = !M.shuffle; paintBtn(); };
    $("#ny-repeat").onclick = () => { M.repeat = !M.repeat; paintBtn(); };
    $("#ny-seek").oninput = (e) => { if (audio.duration && M.seekable && !M.live) audio.currentTime = (e.target.value / 100) * audio.duration; };
    $("#ny-vol").oninput = (e) => { audio.volume = +e.target.value; };
  }

  // ---- CAVA-style visualizer ----------------------------------------------
  function startViz() {
    try {
      if (!actx) {
        actx = new (window.AudioContext || window.webkitAudioContext)();
        vizSource = actx.createMediaElementSource(audio);
        analyser = actx.createAnalyser();
        analyser.fftSize = 128;
        analyser.smoothingTimeConstant = 0.78;
        vizSource.connect(analyser);
        analyser.connect(actx.destination);
        vizData = new Uint8Array(analyser.frequencyBinCount);
      }
      if (actx.state === "suspended") actx.resume();
      if (!vizRAF) drawViz();
    } catch (e) { /* Web Audio unavailable — fine, sound still plays */ }
  }
  function drawViz() {
    vizRAF = requestAnimationFrame(drawViz);
    const cv = $("#ny-viz"); if (!cv || !analyser) return;
    const ctx = cv.getContext("2d"); const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    if (audio.paused) { cancelAnimationFrame(vizRAF); vizRAF = 0; return; }
    analyser.getByteFrequencyData(vizData);
    const bars = 28, step = Math.floor(vizData.length / bars) || 1, bw = w / bars;
    const col = (getComputedStyle(document.documentElement).getPropertyValue("--sc") || "#ff5500").trim();
    for (let i = 0; i < bars; i++) {
      let v = 0; for (let j = 0; j < step; j++) v += vizData[i * step + j] || 0; v /= step;
      const bh = Math.max(2, (v / 255) * h);
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.35 + 0.65 * (v / 255);
      ctx.fillRect(i * bw + 1, h - bh, bw - 2, bh);
    }
    ctx.globalAlpha = 1;
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
    $("#ny-repeat").classList.toggle("act", M.repeat);
    if (S.view === "music") renderTab();
  }

  // ---- playback -----------------------------------------------------------
  const trackKey = (t) => (t.kind === "sc" || t.url ? t.url : t.path);
  const srcFor = (t) => (t.kind === "sc" || t.url)
    ? "/api/music/sc-stream?url=" + encodeURIComponent(t.url)
    : "/api/music/track?path=" + encodeURIComponent(t.path);
  const titleOf = (t) => (t.kind === "sc" || t.url ? t.title : t.name) || "Untitled";
  const subOf = (t) => (t.kind === "sc" || t.url ? (t.artist || "SoundCloud") : (t.folder || "music"));

  function playQueue(tracks, i) {
    M.queue = (tracks || []).slice();
    play(i || 0);
  }
  function play(i) {
    ensureBar();
    if (i < 0 || i >= M.queue.length) return;
    M.idx = i; M.live = false;
    const t = M.queue[i];
    M.seekable = !(t.kind === "sc" || t.url);     // SoundCloud streams aren't seekable
    audio.src = srcFor(t);
    audio.play().catch(() => {});
    $("#ny-np-title").textContent = titleOf(t);
    $("#ny-np-sub").textContent = subOf(t);
    $("#ny-live-pill").hidden = true;
    bar.classList.add("on");
    paintBtn();
  }
  function onEnded() {
    if (M.live) return;
    if (M.repeat) { play(M.idx); return; }
    next();
  }
  function next() {
    if (M.shuffle && M.queue.length > 1) { play(Math.floor(Math.random() * M.queue.length)); return; }
    if (M.idx + 1 < M.queue.length) play(M.idx + 1); else audio.pause();
  }
  function stop() {
    if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
    if (bar) bar.classList.remove("on");
    M.live = false; M.idx = -1;
    if (S.view === "music") renderTab();
  }
  function playLive() {
    ensureBar();
    M.live = true; M.idx = -1; M.seekable = false;
    audio.src = "/api/audio/live?t=" + Date.now();
    audio.play().catch(() => toast("Couldn't start the live audio."));
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

  // ---- tabs ---------------------------------------------------------------
  function renderPage() {
    const el = $("#view"); if (!el || S.view !== "music") return;
    const d = M.data || {};
    M.scLikes = Array.isArray((d.sc || {}).likes) ? d.sc.likes : [];
    M.playlists = Array.isArray(d.playlists) ? d.playlists : [];
    const tabs = [["search", "Search"], ["playlists", "Playlists"], ["likes", "Likes"], ["library", "Library"], ["settings", "Settings"]];
    NY.freshHTML(el, html`
      <div class="row" style="align-items:center;gap:10px;margin-bottom:6px">
        <div style="font-size:22px;font-weight:800;letter-spacing:.3px">nodecloud <span style="color:var(--sc,#ff5500)">♪</span></div>
        <div class="faint small">your own SoundCloud, served by the cluster</div>
      </div>
      <div class="sc-tabs">${raw(tabs.map(([k, n]) => '<button data-tab="' + k + '" class="' + (M.tab === k ? "on" : "") + '">' + n + "</button>").join(""))}</div>
      <div id="sc-tab"></div>`);
    renderTab();
  }

  function renderTab() {
    if (S.view !== "music") return;
    const el = $("#sc-tab"); if (!el || !M.data) return;
    if (M.tab === "search") renderSearch(el);
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
      : (M._searched ? html`<div class="empty">No results.</div>` : "");
    setHTML(el, html`
      <div class="card">
        <div class="row" style="gap:8px"><input class="input" data-music="sc-query" placeholder="Search SoundCloud for a song, artist…" spellcheck="false" style="flex:1">
          <button class="btn primary" data-music="sc-search" ${sc.search_ready ? "" : "disabled"}>Search</button></div>
        ${!sc.search_ready ? html`<p class="small mt">The search helper isn't installed yet. Run <span class="mono">sc-pick</span> once from the server's Terminal, or use <b>Settings → Update everything</b>.</p>` : ""}
        ${!sc.stream_ready ? html`<p class="small faint mt">In-browser playback needs yt-dlp + ffmpeg on the server — run <b>Settings → Update everything</b>. Until then use ＋ to save tracks and Open to listen on SoundCloud.</p>` : ""}
        <div id="sc-results" class="mt">${results}</div>
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
      <a class="btn small" href="${t.url}" target="_blank" rel="noopener noreferrer" title="Open on SoundCloud">↗</a>`;
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
    return [M.queue, M.scResults, M.scLikes, (M.scSet && M.scSet.tracks) || [], (M.openPl && M.openPl.tracks) || [], (M.data || {}).tracks || []]
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
    const el = $("#sc-results"); if (el) setHTML(el, html`<div class="empty"><div class="spin"></div>Searching…</div>`);
    try {
      const r = await getJSON("/api/music/search?q=" + encodeURIComponent(query));
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

  // ---- events -------------------------------------------------------------
  document.addEventListener("click", async (e) => {
    const pb = e.target.closest && e.target.closest("[data-play]");
    if (pb) { startPlay(pb.dataset.play); return; }
    const t = e.target.closest && e.target.closest("[data-music], [data-tab]"); if (!t) return;
    if (t.dataset.tab) { M.tab = t.dataset.tab; M.openPl = null; renderPage(); return; }
    const act = t.dataset.music;
    if (act === "listen") playLive();
    else if (act === "save-dir") await saveDir($('[data-music="dir"]').value.trim());
    else if (act === "browse") openBrowse($('[data-music="dir"]').value.trim());
    else if (act === "b-open" || act === "b-short") openBrowse(t.dataset.path);
    else if (act === "b-cancel") closeBrowse();
    else if (act === "b-use") { closeBrowse(); await saveDir(t.dataset.path); }
    else if (act === "sc-search") await searchSc();
    else if (act === "load-set") await loadSet();
    else if (act === "play-set") { if (M.scSet) playQueue(M.scSet.tracks, 0); }
    else if (act === "likes-play") playQueue(M.scLikes, 0);
    else if (act === "lib-play") playQueue(filteredLib(), 0);
    else if (act === "sc-like") await toggleLike(t.dataset.key);
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
    else if (kind === "lib") { const lib = filteredLib(); const i = lib.findIndex((x) => trackKey(x) === rest); if (i >= 0) playQueue(lib, i); }
  }
  async function toggleLike(url) {
    const track = findTrack(url); if (!track) return;
    const liked = !M.scLikes.some((x) => x.url === url);
    const r = await postJSON("/api/music/like", { track, liked });
    if (!r.ok) { toast(r.error || "Couldn't save that like."); return; }
    M.scLikes = Array.isArray(r.likes) ? r.likes : []; renderTab();
  }
  async function openPlaylist(id) {
    const r = await getJSON("/api/music/playlists?id=" + encodeURIComponent(id));
    if (r.ok && r.playlist) { M.openPl = r.playlist; renderTab(); } else toast("Couldn't open that playlist.");
  }

  document.addEventListener("input", (e) => {
    if (e.target.matches('[data-music="lib-filter"]')) { M._libq = e.target.value; renderTab(); const inp = $('[data-music="lib-filter"]'); if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); } }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.target.matches('[data-music="sc-query"]')) { e.preventDefault(); searchSc(); }
    else if (e.target.matches('[data-music="set-url"]')) { e.preventDefault(); loadSet(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    if (t.dataset.music === "fw-toggle") { const r = await postJSON("/api/music/config", { forward_enabled: t.checked }); if (r.ok) { await load(); renderTab(); } }
    else if (t.dataset.music === "fw-source") { await postJSON("/api/music/config", { forward_source: t.value }); await load(); renderTab(); }
  });

  V.music = {
    build() { ensureBar(); NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(renderPage); },
    update() { },
  };
})();

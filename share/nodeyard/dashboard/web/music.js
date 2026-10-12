// The Music page and the persistent player bar. The server streams a folder of music (with seeking) and, when the
// admin turns it on, forwards the host's own audio output as live MP3 — both so a browser only needs the dashboard.
// Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, toast, getJSON, postJSON } = U;

  const M = { data: null, queue: [], idx: -1, live: false };
  let audio = null, bar = null;

  // ---- the persistent player bar ------------------------------------------
  function ensureBar() {
    if (bar) return;
    const style = document.createElement("style");
    style.textContent =
      "#ny-player{position:fixed;left:0;right:0;bottom:0;z-index:60;display:none;align-items:center;gap:14px;" +
      "padding:9px 16px;background:var(--card,#131a2e);border-top:1px solid var(--line,rgba(125,160,200,.18));" +
      "box-shadow:0 -8px 28px rgba(0,0,0,.35);font-size:13px}" +
      "#ny-player.on{display:flex}" +
      "#ny-player .np{min-width:160px;max-width:260px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}" +
      "#ny-player .np b{display:block;overflow:hidden;text-overflow:ellipsis}" +
      "#ny-player .np span{color:var(--ink3,#8fa3c0);font-size:11px}" +
      "#ny-player button{background:none;border:0;color:inherit;cursor:pointer;font-size:17px;padding:4px;border-radius:6px}" +
      "#ny-player button:hover{background:rgba(125,160,200,.14)}" +
      "#ny-player .seek{flex:1;display:flex;align-items:center;gap:8px;min-width:120px}" +
      "#ny-player input[type=range]{flex:1;accent-color:var(--acc,#7c8cff)}" +
      "#ny-player .t{font-variant-numeric:tabular-nums;color:var(--ink3,#8fa3c0);font-size:11px;min-width:40px}" +
      "#ny-player .live-pill{color:#ff5a6a;font-weight:600;font-size:11px;letter-spacing:1px}" +
      "@media (max-width:760px){#ny-player{bottom:56px}#ny-player .np{max-width:120px}}";
    document.head.appendChild(style);
    bar = document.createElement("div");
    bar.id = "ny-player";
    bar.innerHTML =
      '<div class="np"><b id="ny-np-title">—</b><span id="ny-np-sub"></span></div>' +
      '<button id="ny-prev" title="Previous">⏮</button>' +
      '<button id="ny-play" title="Play/pause">▶</button>' +
      '<button id="ny-next" title="Next">⏭</button>' +
      '<div class="seek"><span class="t" id="ny-cur">0:00</span><input type="range" id="ny-seek" min="0" max="100" value="0" step="0.1" aria-label="Seek"><span class="t" id="ny-dur">0:00</span></div>' +
      '<span class="live-pill" id="ny-live-pill" hidden>● LIVE</span>' +
      '<button id="ny-vol-i" title="Volume">🔊</button><input type="range" id="ny-vol" min="0" max="1" value="1" step="0.05" style="width:80px" aria-label="Volume">' +
      '<button id="ny-close" title="Close player">✕</button>';
    document.body.appendChild(bar);
    audio = new Audio();
    audio.preload = "metadata";
    audio.addEventListener("timeupdate", tick);
    audio.addEventListener("durationchange", tick);
    audio.addEventListener("ended", () => { if (!M.live) next(); });
    audio.addEventListener("play", paintBtn);
    audio.addEventListener("pause", paintBtn);
    $("#ny-play").onclick = () => { if (audio.paused) audio.play(); else audio.pause(); };
    $("#ny-prev").onclick = () => { if (!M.live) play(M.idx - 1); };
    $("#ny-next").onclick = () => { if (!M.live) next(); };
    $("#ny-close").onclick = stop;
    $("#ny-seek").oninput = (e) => { if (audio.duration && !M.live) audio.currentTime = (e.target.value / 100) * audio.duration; };
    $("#ny-vol").oninput = (e) => { audio.volume = +e.target.value; };
  }

  const fmtTime = (s) => (isFinite(s) ? Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0") : "0:00");
  function tick() {
    if (!bar) return;
    $("#ny-cur").textContent = fmtTime(audio.currentTime);
    $("#ny-dur").textContent = M.live ? "live" : fmtTime(audio.duration);
    const sk = $("#ny-seek");
    sk.disabled = M.live;
    if (!M.live && audio.duration) sk.value = (audio.currentTime / audio.duration) * 100;
  }
  function paintBtn() { if (bar) $("#ny-play").textContent = audio.paused ? "▶" : "⏸"; if (S.view === "music") renderLib(); }

  function play(i) {
    ensureBar();
    if (i < 0 || i >= M.queue.length) return;
    M.idx = i; M.live = false;
    const t = M.queue[i];
    audio.src = "/api/music/track?path=" + encodeURIComponent(t.path);
    audio.play().catch(() => {});
    $("#ny-np-title").textContent = t.name;
    $("#ny-np-sub").textContent = t.folder || "music";
    $("#ny-live-pill").hidden = true;
    bar.classList.add("on");
    paintBtn();
  }
  function next() { if (M.idx + 1 < M.queue.length) play(M.idx + 1); else { audio.pause(); } }
  function stop() { if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); } if (bar) bar.classList.remove("on"); M.live = false; M.idx = -1; if (S.view === "music") renderLib(); }

  function playLive() {
    ensureBar();
    M.live = true; M.idx = -1;
    audio.src = "/api/audio/live?t=" + Date.now();
    audio.play().catch(() => toast("Couldn't start the live audio."));
    $("#ny-np-title").textContent = "Server audio";
    $("#ny-np-sub").textContent = "live from the host";
    $("#ny-live-pill").hidden = false;
    bar.classList.add("on");
    paintBtn();
  }

  // ---- the Music page -----------------------------------------------------
  function renderLib() {
    if (S.view !== "music") return;
    const el = $("#music-lib"); if (!el || !M.data) return;
    const tracks = M.data.tracks || [];
    M.queue = tracks;
    if (!tracks.length) { setHTML(el, html`<div class="empty">No audio files in that folder yet.</div>`); return; }
    setHTML(el, html`${tracks.map((t, i) => html`<div class="row" style="justify-content:space-between;padding:7px 2px;border-bottom:1px solid var(--line)">
      <button class="btn small ${i === M.idx && !M.live ? "primary" : ""}" data-music="play" data-i="${i}" style="min-width:34px">${i === M.idx && !M.live && audio && !audio.paused ? "⏸" : "▶"}</button>
      <div style="flex:1;margin:0 10px;overflow:hidden"><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.name)}</div>${t.folder ? html`<div class="faint small">${esc(t.folder)}</div>` : ""}</div>
      <div class="faint small">${(t.size / 1048576).toFixed(1)} MB</div></div>`)}`);
  }

  function renderPage() {
    const el = $("#view"); if (!el || S.view !== "music") return;
    const d = M.data || {};
    const fw = d.forward || {};
    const sc = d.sc || {};
    const srcOpts = ['<option value="">Automatic (the server\'s output)</option>']
      .concat((fw.sources || []).map((s) => '<option value="' + esc(s) + '"' + (s === fw.source ? " selected" : "") + ">" + esc(s) + "</option>")).join("");
    const fwBody = !fw.ffmpeg
      ? html`<p class="muted small">ffmpeg isn't installed on the server, so live audio forwarding isn't available.${fw.reason ? " " + esc(fw.reason) : ""}</p>`
      : html`<label class="check"><input type="checkbox" data-music="fw-toggle" ${fw.enabled ? raw("checked") : ""}> <span>Forward the server's audio output to the browser — captured <b>automatically</b></span></label>
        <div class="small faint mt">Capturing: <span class="mono">${esc(fw.auto_source || "auto")}</span>${fw.source ? " (chosen)" : " (auto — whatever the server is playing)"}</div>
        ${fw.reason ? html`<p class="err small mt">${esc(fw.reason)}</p>` : ""}
        <div class="row mt" style="gap:8px;align-items:center">
          <button class="btn small" data-music="fw-test">Test capture</button>
          ${fw.enabled && fw.available ? raw('<button class="btn small primary" data-music="listen">Listen now</button>') : ""}
        </div>
        <details class="mt"><summary class="small faint">Choose a source instead of automatic</summary>
          ${raw('<select class="select mt" data-music="fw-source">' + srcOpts + "</select>")}</details>
        <div id="fw-test-out" class="small mt"></div>`;
    const scBody = html`<p class="muted small" style="margin-top:0">Paste a SoundCloud track or set URL; the server downloads it with yt-dlp (what your <span class="mono">sc-pick</span> uses) into the music folder, then it appears below. Only download tracks you have the right to.</p>
      <div class="row" style="gap:8px"><input class="input" data-music="sc-url" placeholder="https://soundcloud.com/artist/track" spellcheck="false" style="flex:1">
        <button class="btn primary" data-music="sc-go" ${sc.ready ? "" : "disabled"}>Download</button></div>
      ${!sc.yt_dlp ? html`<p class="small mt">The downloader isn't set up yet. <button class="btn small primary" data-music="sc-setup">Set it up automatically</button></p>`
        : (!d.dir ? html`<p class="small faint mt">Set a music folder above first.</p>` : "")}
      <div id="sc-out" class="small mt" style="white-space:pre-wrap;max-height:200px;overflow:auto"></div>`;
    NY.freshHTML(el, html`
      ${card("Music folder", html`<p class="muted small" style="margin-top:0">The server plays audio files from one folder on the host (the control node, not your Mac). Everyone signed in can listen.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="dir" value="${esc(d.dir || "")}" placeholder="/home/barnaby/Music/soundcloud" spellcheck="false" style="flex:1">
        <button class="btn" data-music="browse">Browse…</button><button class="btn primary" data-music="save-dir">Save</button></div>`)}
      ${card("SoundCloud (sc-pick)", scBody)}
      ${card("Live audio forwarding", fwBody)}
      <div class="section-title">Library</div>
      <div class="card"><div id="music-lib"><div class="empty"><div class="spin"></div>Loading…</div></div></div>`);
    renderLib();
  }

  // ---- graphical folder picker -------------------------------------------
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
  function closeBrowse() { const m = document.getElementById("music-browse"); if (m) m.remove(); }
  async function saveDir(dir) {
    const r = await postJSON("/api/music/config", { dir }); if (r.ok) { await load(); renderPage(); toast("Saved."); } else toast(r.error || "Not saved.");
  }

  async function load() { try { const r = await getJSON("/api/music"); if (r.ok) M.data = r; } catch (e) { /* signed out */ } }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    const act = t.dataset.music;
    if (act === "play") { play(+t.dataset.i); }
    else if (act === "listen") { playLive(); }
    else if (act === "save-dir") { await saveDir($('[data-music="dir"]').value.trim()); }
    else if (act === "browse") { openBrowse($('[data-music="dir"]').value.trim()); }
    else if (act === "b-open" || act === "b-short") { openBrowse(t.dataset.path); }
    else if (act === "b-cancel") { closeBrowse(); }
    else if (act === "b-use") { closeBrowse(); await saveDir(t.dataset.path); }
    else if (act === "fw-test") {
      const out = $("#fw-test-out"); if (out) out.textContent = "Testing…";
      const r = await postJSON("/api/audio/test", {}); if (out) out.innerHTML = (r.ok ? '<span style="color:var(--ok,#3ecf8e)">✓ </span>' : '<span class="err">✕ </span>') + esc(r.message || r.error || "");
    }
    else if (act === "sc-setup") {
      const out = $("#sc-out"); if (out) out.textContent = "Setting up (downloading yt-dlp)…";
      const r = await postJSON("/api/music/sc-setup", {}); if (out) out.textContent = r.message || r.error || ""; await load(); renderPage();
    }
    else if (act === "sc-go") {
      const url = ($('[data-music="sc-url"]').value || "").trim(); if (!url) return;
      const out = $("#sc-out"); if (out) out.textContent = "Downloading… (this can take a minute)";
      const r = await postJSON("/api/music/sc", { url }); if (out) out.textContent = r.text || r.error || "";
      await load(); if (S.view === "music") renderLib();
    }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    if (t.dataset.music === "fw-toggle") { const r = await postJSON("/api/music/config", { forward_enabled: t.checked }); if (r.ok) { await load(); renderPage(); } }
    else if (t.dataset.music === "fw-source") { await postJSON("/api/music/config", { forward_source: t.value }); await load(); renderPage(); }
  });

  V.music = {
    build() { NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(renderPage); },
    update() { },
  };
})();

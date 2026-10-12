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
    const fwBody = !fw.ffmpeg
      ? html`<p class="muted small">ffmpeg isn't installed on the server, so live audio forwarding isn't available. Install ffmpeg (and PulseAudio/PipeWire) on the host to use it.</p>`
      : html`<label class="check"><input type="checkbox" data-music="fw-toggle" ${fw.enabled ? raw("checked") : ""}> <span>Forward the server's audio output to the browser</span></label>
        <label class="field-l" style="margin-top:8px">Capture source${raw('<select class="select" data-music="fw-source">' + ['<option value="">Default</option>'].concat((fw.sources || []).map((s) => '<option value="' + esc(s) + '"' + (s === fw.source ? " selected" : "") + ">" + esc(s) + "</option>")).join("") + "</select>")}</label>
        <p class="faint small">Pick the <span class="mono">.monitor</span> source to capture what the server is playing. ${fw.enabled && fw.available ? raw('<button class="btn small primary" data-music="listen" style="margin-left:6px">Listen now</button>') : ""}</p>`;
    NY.freshHTML(el, html`
      ${card("Music folder", html`<p class="muted small" style="margin-top:0">The server plays audio files from one folder on the host. Set its path; everyone signed in to the dashboard can listen.</p>
        <div class="row" style="gap:8px"><input class="input" data-music="dir" value="${esc(d.dir || "")}" placeholder="/srv/music" spellcheck="false" style="flex:1">
        <button class="btn primary" data-music="save-dir">Save</button></div>`)}
      ${card("Live audio forwarding", fwBody)}
      <div class="section-title">Library</div>
      <div class="card"><div id="music-lib"><div class="empty"><div class="spin"></div>Loading…</div></div></div>`);
    renderLib();
  }

  async function load() { try { const r = await getJSON("/api/music"); if (r.ok) M.data = r; } catch (e) { /* signed out */ } }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    const act = t.dataset.music;
    if (act === "play") { play(+t.dataset.i); }
    else if (act === "listen") { playLive(); }
    else if (act === "save-dir") {
      const dir = $('[data-music="dir"]').value.trim();
      const r = await postJSON("/api/music/config", { dir }); if (r.ok) { await load(); renderPage(); toast("Saved."); } else toast(r.error || "Not saved.");
    }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target.closest && e.target.closest("[data-music]"); if (!t) return;
    if (t.dataset.music === "fw-toggle") { const r = await postJSON("/api/music/config", { forward_enabled: t.checked }); if (r.ok) { await load(); renderPage(); } }
    else if (t.dataset.music === "fw-source") { await postJSON("/api/music/config", { forward_source: t.value }); await load(); }
  });

  V.music = {
    build() { NY.freshHTML($("#view"), html`<div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div>`); load().then(renderPage); },
    update() { },
  };
})();

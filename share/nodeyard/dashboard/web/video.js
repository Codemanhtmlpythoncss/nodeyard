// The Video page — upload and watch movies and series, streamed from the server with HTTP Range so seeking works.
// Playback position is saved per item (which episode, and how far in), so Continue watching and auto-resume land you
// where you left off; a series auto-advances to the next episode. A whole season can be uploaded as one .zip.
// Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, toast, getJSON, postJSON } = U;

  const VD = {
    data: { library: [] },
    nowItem: null, nowEp: null,     // what's playing
    lastSave: 0, open: {},          // expanded series ids
    uploading: false,
  };
  const CH = 2 * 1024 * 1024;       // raw bytes per upload piece (server piece limit is 4 MB)

  // ---- small helpers -----------------------------------------------------
  const fileUrl = (path) => "/api/video/file?path=" + encodeURIComponent(path);
  function fmtTime(s) {
    s = Math.max(0, Math.round(+s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  }
  function fmtSize(b) { b = +b || 0; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; } return b.toFixed(b < 10 && i > 0 ? 1 : 0) + " " + u[i]; }
  const itemOf = (id) => (VD.data.library || []).find((i) => i.id === id);
  const epOf = (item, epId) => (item && item.episodes || []).find((e) => e.id === epId);
  const pct = (pr) => (pr && pr.duration > 0 ? Math.max(0, Math.min(100, (pr.time / pr.duration) * 100)) : 0);
  const inProgress = (pr) => !!(pr && !pr.finished && pr.time > 1);

  function resumeEpisode(item) {
    // the episode to resume/start for a series: last watched if unfinished, else first unfinished, else first
    const eps = item.episodes || []; if (!eps.length) return null;
    if (item.last_episode) { const e = epOf(item, item.last_episode); if (e && !(e.progress || {}).finished) return e; }
    return eps.find((e) => !(e.progress || {}).finished) || eps[eps.length - 1] || eps[0];
  }
  function continueList() {
    const out = [];
    (VD.data.library || []).forEach((it) => {
      if (it.kind === "movie") { if (inProgress(it.progress)) out.push({ item: it, ep: null, pr: it.progress }); }
      else { const e = resumeEpisode(it); if (e && inProgress(e.progress)) out.push({ item: it, ep: e, pr: e.progress }); }
    });
    return out;
  }

  // ---- the player --------------------------------------------------------
  let vEl = null;
  function stage() { return $("#vd-stage"); }

  function playItem(itemId, epId) {
    const item = itemOf(itemId); if (!item) return;
    if (VD.nowItem) saveProgress(true);   // bank the outgoing video's position first
    let path, title, sub, pr;
    if (item.kind === "movie") {
      path = item.path; title = item.title; sub = "Movie"; pr = item.progress || {}; epId = null;
    } else {
      const ep = epId ? epOf(item, epId) : resumeEpisode(item);
      if (!ep) return;
      epId = ep.id; path = ep.path; title = item.title;
      sub = "S" + ep.season + "·E" + ep.episode + (ep.title && ep.title !== "Episode" ? " — " + ep.title : "");
      pr = ep.progress || {};
    }
    VD.nowItem = itemId; VD.nowEp = epId; VD.lastSave = 0;
    setHTML(stage(), html`
      <div class="vd-now">
        <video id="vd-video" controls autoplay playsinline preload="metadata" style="width:100%;max-height:68vh;background:#000;border-radius:12px"></video>
        <div class="row" style="align-items:baseline;gap:10px;margin-top:8px">
          <div style="min-width:0"><div style="font-weight:700;font-size:16px">${title}</div><div class="faint small">${sub}</div></div>
          <span class="grow"></span>
          ${item.kind === "series" ? html`<button class="btn small" data-vd="restart">↺ From start</button>` : ""}
          <button class="btn small" data-vd="close-player">Close</button>
        </div>
      </div>`);
    vEl = $("#vd-video");
    vEl.src = fileUrl(path);
    const resumeAt = pr && !pr.finished ? +pr.time || 0 : 0;
    vEl.addEventListener("loadedmetadata", () => { if (resumeAt > 0 && resumeAt < (vEl.duration || 1e9) - 2) { try { vEl.currentTime = resumeAt; } catch (e) {} } }, { once: true });
    vEl.addEventListener("timeupdate", () => saveProgress(false));
    vEl.addEventListener("pause", () => saveProgress(true));
    vEl.addEventListener("ended", onEnded);
    vEl.play().catch(() => {});
    try { stage().scrollIntoView({ block: "start", behavior: "smooth" }); } catch (e) {}
    renderLib();   // reflect "now playing" highlight
  }

  function saveProgress(force) {
    if (!vEl || !VD.nowItem) return;
    const now = Date.now();
    if (!force && now - VD.lastSave < 4000) return;
    VD.lastSave = now;
    const t = vEl.currentTime || 0, dur = isFinite(vEl.duration) ? vEl.duration : 0;
    if (dur <= 0) return;
    const body = { item_id: VD.nowItem, episode_id: VD.nowEp || "", time: t, duration: dur };
    // update local copy so the library reflects it without a round-trip
    const item = itemOf(VD.nowItem);
    if (item) { const p = item.kind === "movie" ? item : epOf(item, VD.nowEp); if (p) p.progress = { time: t, duration: dur, finished: t >= dur - 15 }; }
    postJSON("/api/video/progress", body).catch(() => {});
  }

  function onEnded() {
    if (!VD.nowItem) return;
    const item = itemOf(VD.nowItem);
    postJSON("/api/video/progress", { item_id: VD.nowItem, episode_id: VD.nowEp || "", time: vEl.duration || 0, duration: vEl.duration || 0, finished: true }).catch(() => {});
    if (item) { const p = item.kind === "movie" ? item : epOf(item, VD.nowEp); if (p) p.progress = Object.assign({}, p.progress, { finished: true }); }
    if (item && item.kind === "series") {
      const eps = item.episodes || [], i = eps.findIndex((e) => e.id === VD.nowEp);
      if (i >= 0 && i + 1 < eps.length) { toast("Up next: S" + eps[i + 1].season + "·E" + eps[i + 1].episode); playItem(item.id, eps[i + 1].id); return; }
    }
    renderLib();
  }

  function closePlayer() {
    saveProgress(true);
    if (vEl) { try { vEl.pause(); } catch (e) {} vEl.removeAttribute("src"); vEl.load && vEl.load(); }
    vEl = null; VD.nowItem = VD.nowEp = null;
    setHTML(stage(), "");
    renderLib();
  }

  // ---- library rendering -------------------------------------------------
  function progBar(pr) {
    const p = pct(pr); if (p <= 0 && !(pr && pr.finished)) return "";
    return html`<div style="height:4px;border-radius:999px;background:var(--line,#2a3550);margin-top:6px;overflow:hidden"><i style="display:block;height:100%;width:${pr && pr.finished ? 100 : p}%;background:${pr && pr.finished ? "var(--good,#39d98a)" : "var(--accent,#5b9bff)"}"></i></div>`;
  }
  const isNow = (id, ep) => VD.nowItem === id && (VD.nowEp || null) === (ep || null);

  function movieCard(it) {
    return html`<div class="card vd-card ${isNow(it.id, null) ? "vd-on" : ""}" style="padding:12px">
      <div class="row" style="align-items:flex-start;gap:8px">
        <button class="vd-poster" data-vd="play" data-id="${it.id}" aria-label="Play ${it.title}"><svg class="icon"><use href="#i-play"/></svg></button>
        <div class="grow" style="min-width:0">
          <div style="font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${it.title}</div>
          <div class="faint small">Movie · ${fmtSize(it.size)}${(it.progress || {}).finished ? " · watched" : inProgress(it.progress) ? " · " + fmtTime(it.progress.time) + " in" : ""}</div>
          ${progBar(it.progress)}
        </div>
        <button class="btn small ghost" data-vd="rename" data-id="${it.id}" title="Rename" aria-label="Rename">✎</button>
        <button class="btn small ghost" data-vd="del" data-id="${it.id}" title="Delete" aria-label="Delete">✕</button>
      </div></div>`;
  }

  function seriesCard(it) {
    const open = !!VD.open[it.id], eps = it.episodes || [];
    const watched = eps.filter((e) => (e.progress || {}).finished).length;
    const bySeason = {};
    eps.forEach((e) => { (bySeason[e.season] = bySeason[e.season] || []).push(e); });
    return html`<div class="card" style="padding:12px">
      <div class="row" style="align-items:center;gap:8px">
        <button class="vd-poster" data-vd="play" data-id="${it.id}" aria-label="Resume ${it.title}"><svg class="icon"><use href="#i-play"/></svg></button>
        <button class="grow" data-vd="toggle" data-id="${it.id}" style="text-align:left;background:none;border:0;color:inherit;cursor:pointer;min-width:0">
          <div style="font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${it.title}</div>
          <div class="faint small">Series · ${eps.length} episode${eps.length === 1 ? "" : "s"} · ${watched}/${eps.length} watched ${open ? "▾" : "▸"}</div>
        </button>
        <button class="btn small ghost" data-vd="rename" data-id="${it.id}" title="Rename series" aria-label="Rename series">✎</button>
        <button class="btn small ghost" data-vd="del" data-id="${it.id}" title="Delete series" aria-label="Delete series">✕</button>
      </div>
      ${open ? html`<div class="mt" style="margin-left:6px;border-left:2px solid var(--line,#2a3550);padding-left:10px">
        ${Object.keys(bySeason).sort((a, b) => a - b).map((sn) => html`
          <div class="faint small" style="margin:8px 0 4px;text-transform:uppercase;letter-spacing:.06em">Season ${sn}</div>
          ${bySeason[sn].map((e) => html`<div class="row vd-ep ${isNow(it.id, e.id) ? "vd-on" : ""}" style="align-items:center;gap:8px;padding:5px 0">
            <button class="btn small" data-vd="play" data-id="${it.id}" data-ep="${e.id}" style="min-width:54px">${isNow(it.id, e.id) ? "▶ now" : "E" + e.episode}</button>
            <div class="grow" style="min-width:0">
              <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.title && e.title !== "Episode" ? e.title : "Episode " + e.episode}</div>
              <div class="faint small">${fmtSize(e.size)}${(e.progress || {}).finished ? " · watched" : inProgress(e.progress) ? " · " + fmtTime(e.progress.time) + " in" : ""}</div>
              ${progBar(e.progress)}
            </div>
            <button class="btn small ghost" data-vd="rename" data-id="${it.id}" data-ep="${e.id}" title="Rename episode" aria-label="Rename episode">✎</button>
            <button class="btn small ghost" data-vd="del" data-id="${it.id}" data-ep="${e.id}" title="Delete episode" aria-label="Delete episode">✕</button>
          </div>`)}
        `)}
      </div>` : ""}
    </div>`;
  }

  function renderLib() {
    const el = $("#vd-lib"); if (!el) return;
    const lib = VD.data.library || [];
    const movies = lib.filter((i) => i.kind === "movie");
    const series = lib.filter((i) => i.kind === "series");
    const cont = continueList();
    setHTML(el, html`
      ${cont.length ? html`<div class="section-title">Continue watching</div>
        <div class="grid g-3">${cont.map(({ item, ep, pr }) => html`
          <button class="card vd-cont" data-vd="play" data-id="${item.id}" ${ep ? 'data-ep="' + ep.id + '"' : ""} style="text-align:left;cursor:pointer;padding:12px">
            <div style="font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${item.title}</div>
            <div class="faint small">${ep ? "S" + ep.season + "·E" + ep.episode : "Movie"} · ${fmtTime(pr.time)} / ${fmtTime(pr.duration)}</div>
            ${progBar(pr)}
          </button>`)}</div>` : ""}

      ${movies.length ? html`<div class="section-title">Movies <span class="count">${movies.length}</span></div>
        <div class="grid g-2">${movies.map(movieCard)}</div>` : ""}

      ${series.length ? html`<div class="section-title">Series <span class="count">${series.length}</span></div>
        <div class="grid" style="gap:10px">${series.map(seriesCard)}</div>` : ""}

      ${!lib.length ? html`<div class="card"><div class="empty">Nothing here yet. Upload a movie, an episode, or a whole season as a .zip above.</div></div>` : ""}`);
  }

  // ---- upload ------------------------------------------------------------
  async function uploadOne(file, onProgress) {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const id = (window.crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : (Date.now().toString(16) + Math.random().toString(16).slice(2))).slice(0, 12);
    let offset = 0; const total = file.size;
    if (!total) throw new Error(file.name + " is empty.");
    try {
      do {
        const slice = file.slice(offset, Math.min(offset + CH, total));
        const buf = new Uint8Array(await slice.arrayBuffer());
        let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
        const done = offset + slice.size >= total;
        const r = await postJSON("/api/video/upload", { id, ext, name: file.name, offset, data: btoa(bin), done });
        if (!r || !r.ok) throw new Error((r && r.error) || "Upload failed.");
        offset += slice.size;
        if (onProgress) onProgress(offset, total);
        if (done) return r;   // {path, name, size}
      } while (offset < total);
    } catch (e) {
      try { await postJSON("/api/video/upload-cancel", { id }); } catch (ignore) {}
      throw e;
    }
  }

  function upMsg(text, cls) { const m = $("#vd-upmsg"); if (m) { m.textContent = text || ""; m.className = "small mt " + (cls || "faint"); } }

  async function doUpload() {
    if (VD.uploading) return;
    const fileEl = $("#vd-file"), file = fileEl && fileEl.files && fileEl.files[0];
    if (!file) { upMsg("Choose a file first.", "bad-t"); return; }
    const kind = ($('input[name="vd-kind"]:checked') || {}).value || "movie";
    const isArchive = /\.(zip|tar|tgz|tbz2?|txz|tar\.(gz|bz2|xz))$/i.test(file.name);
    if (kind === "movie" && isArchive) { upMsg("An archive is for a series, not a movie. Choose Series, or pick a single video file.", "bad-t"); return; }
    const seriesTitle = ($("#vd-series") && $("#vd-series").value || "").trim();
    if (kind === "series" && !seriesTitle) { upMsg("Enter a series name.", "bad-t"); return; }

    VD.uploading = true; const btn = $("#vd-upbtn"); if (btn) btn.disabled = true;
    try {
      upMsg("Uploading " + file.name + "… 0%");
      const r = await uploadOne(file, (sent, total) => upMsg("Uploading " + file.name + "… " + Math.round(sent / total * 100) + "%"));
      upMsg("Adding to your library…");
      let add;
      if (kind === "movie") {
        const title = ($("#vd-title") && $("#vd-title").value || "").trim() || r.name;
        add = await postJSON("/api/video/add", { kind: "movie", title, path: r.path });
      } else if (isArchive) {
        add = await postJSON("/api/video/add-archive", { series_title: seriesTitle, path: r.path });
      } else {
        add = await postJSON("/api/video/add", {
          kind: "series", series_title: seriesTitle, path: r.path,
          season: +($("#vd-season") && $("#vd-season").value || 1),
          episode: +($("#vd-episode") && $("#vd-episode").value || 0) || undefined,
          episode_title: ($("#vd-eptitle") && $("#vd-eptitle").value || "").trim(),
        });
      }
      if (!add || !add.ok) throw new Error((add && add.error) || "Couldn't add it.");
      upMsg(isArchive ? "Added " + (add.count || 0) + " episode" + (add.count === 1 ? "" : "s") + " to “" + seriesTitle + "”." : "Added.", "good-t");
      if (fileEl) fileEl.value = "";
      await load(); renderLib(); refreshSeriesList();
    } catch (e) {
      upMsg(e && e.message || "Upload failed.", "bad-t");
    } finally {
      VD.uploading = false; if (btn) btn.disabled = false;
    }
  }

  function refreshSeriesList() {
    const dl = $("#vd-series-list"); if (!dl) return;
    setHTML(dl, (VD.data.library || []).filter((i) => i.kind === "series").map((i) => raw('<option value="' + esc(i.title) + '"></option>')));
  }

  function uploadForm() {
    return html`<div class="card" style="padding:14px">
      <div class="row" style="align-items:center;gap:10px"><svg class="icon" style="color:var(--accent)"><use href="#i-video"/></svg>
        <b>Upload</b><span class="faint small">movies, single episodes, or a whole season as a .zip or .tar</span></div>
      <div class="row wrap mt" style="gap:14px;align-items:flex-end">
        <label class="row" style="gap:6px"><input type="radio" name="vd-kind" value="movie" checked data-vd="kind"> Movie</label>
        <label class="row" style="gap:6px"><input type="radio" name="vd-kind" value="series" data-vd="kind"> Series</label>
      </div>
      <div class="mt"><input id="vd-file" type="file" accept="video/*,.mkv,.avi,.mov,.ts,.zip,.tar,.tgz,.tbz2,.txz,.gz,.bz2,.xz" class="input"></div>
      <div id="vd-movie-fields" class="mt"><input id="vd-title" class="input" placeholder="Title (optional — defaults to the file name)"></div>
      <div id="vd-series-fields" class="mt" style="display:none">
        <input id="vd-series" class="input" list="vd-series-list" placeholder="Series name (new or existing)" autocomplete="off">
        <datalist id="vd-series-list"></datalist>
        <div class="row wrap mt" style="gap:8px">
          <input id="vd-season" class="input" type="number" min="0" value="1" style="width:110px" placeholder="Season" aria-label="Season">
          <input id="vd-episode" class="input" type="number" min="0" style="width:120px" placeholder="Episode #" aria-label="Episode number">
          <input id="vd-eptitle" class="input" style="min-width:180px;flex:1" placeholder="Episode title (optional)">
        </div>
        <div class="faint small mt">Pick a <b>.zip</b> or <b>.tar</b> (incl. <code>.tar.gz</code>) of videos to add a whole season at once — season/episode numbers are read from the file names (e.g. <code>S01E02</code>).</div>
      </div>
      <div class="row mt" style="gap:10px;align-items:center">
        <button id="vd-upbtn" class="btn" data-vd="upload"><svg class="icon"><use href="#i-download"/></svg> Upload</button>
        <div id="vd-upmsg" class="small faint" role="status" aria-live="polite"></div>
      </div>
    </div>`;
  }

  function toggleKind() {
    const kind = ($('input[name="vd-kind"]:checked') || {}).value || "movie";
    const mv = $("#vd-movie-fields"), sr = $("#vd-series-fields");
    if (mv) mv.style.display = kind === "movie" ? "" : "none";
    if (sr) sr.style.display = kind === "series" ? "" : "none";
  }

  // ---- events ------------------------------------------------------------
  document.addEventListener("click", (e) => {
    if (S.view !== "video") return;
    const t = e.target.closest && e.target.closest("[data-vd]"); if (!t) return;
    const act = t.dataset.vd, id = t.dataset.id, ep = t.dataset.ep || null;
    if (act === "play") { e.preventDefault(); playItem(id, ep); }
    else if (act === "toggle") { VD.open[id] = !VD.open[id]; renderLib(); }
    else if (act === "close-player") closePlayer();
    else if (act === "restart") { if (vEl) { try { vEl.currentTime = 0; vEl.play(); } catch (err) {} } }
    else if (act === "upload") doUpload();
    else if (act === "del") onDelete(id, ep);
    else if (act === "rename") onRename(id, ep);
  });
  document.addEventListener("change", (e) => {
    if (S.view !== "video") return;
    if (e.target.closest && e.target.closest('[data-vd="kind"]')) toggleKind();
  });

  async function onDelete(id, ep) {
    const item = itemOf(id); if (!item) return;
    const what = ep ? "this episode" : item.kind === "series" ? "the whole series “" + item.title + "” and all its episodes" : "“" + item.title + "”";
    if (!confirm("Delete " + what + "? The file will be removed from the server.")) return;
    if (isNow(id, ep) || (!ep && VD.nowItem === id)) closePlayer();
    try { const r = await postJSON("/api/video/delete", { item_id: id, episode_id: ep || "" }); if (!r.ok) throw new Error(r.error); }
    catch (err) { toast(err.message || "Couldn't delete it."); return; }
    await load(); renderLib(); refreshSeriesList();
  }
  async function onRename(id, ep) {
    const item = itemOf(id); if (!item) return;
    const cur = ep ? (epOf(item, ep) || {}).title || "" : item.title || "";
    const title = prompt(ep ? "New episode title:" : item.kind === "series" ? "New series name:" : "New title:", cur);
    if (title == null || !title.trim()) return;
    try { const r = await postJSON("/api/video/rename", { item_id: id, episode_id: ep || "", title: title.trim() }); if (!r.ok) throw new Error(r.error); }
    catch (err) { toast(err.message || "Couldn't rename it."); return; }
    await load(); renderLib();
  }

  // save the position if the page is closed mid-watch (timeupdate only fires every 4s)
  window.addEventListener("beforeunload", () => {
    if (!vEl || !VD.nowItem || !isFinite(vEl.duration) || vEl.duration <= 0) return;
    try {
      const body = JSON.stringify({ item_id: VD.nowItem, episode_id: VD.nowEp || "", time: vEl.currentTime || 0, duration: vEl.duration });
      navigator.sendBeacon && navigator.sendBeacon("/api/video/progress", new Blob([body], { type: "application/json" }));
    } catch (e) {}
  });

  async function load() { try { const r = await getJSON("/api/video"); VD.data = r && r.library ? r : { library: [] }; } catch (e) { VD.data = { library: [] }; } return VD.data; }

  V.video = {
    build() {
      VD.nowItem = VD.nowEp = null; vEl = null;
      NY.freshHTML($("#view"), html`
        <div class="row" style="align-items:center;gap:10px;margin-bottom:8px">
          <div style="font-size:22px;font-weight:800;letter-spacing:.3px">Video</div>
          <div class="faint small">your movies and series, streamed from the cluster</div>
        </div>
        <div id="vd-stage"></div>
        <div class="mt">${uploadForm()}</div>
        <div id="vd-lib" class="mt"><div class="card"><div class="empty"><div class="spin"></div>Loading…</div></div></div>`);
      toggleKind();
      load().then(() => { renderLib(); refreshSeriesList(); });
    },
    update() {},
  };
})();

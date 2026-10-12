// The full-screen wallpaper clock and personal widgets.
(function () {
  "use strict";
  const { esc, html, raw } = NY;
  const U = NY.ui;
  const { S, V, $, go, getJSON, postJSON, toast, store } = U;   // (U.copyText for the chat's copy button)

  const CATALOG = [
    ["player", "Music player", "Play and pick songs without leaving the desk"], ["chat", "AI chat", "Ask your Nodeyard AI from the desk"],
    ["services", "Services", "Which of your watched services are up"], ["system", "System load", "CPU, memory, disk and temperature across the cluster"],
    ["todo", "To-do list", "A small list for today"], ["weather", "Weather", "Conditions for a place you choose"],
    ["calendar", "Calendar", "A month at a glance"], ["notes", "Notes", "A saved scratchpad"],
    ["focus", "Focus timer", "A simple focus and break timer"], ["countdown", "Countdown", "Count down to a date"],
    ["quote", "Daily quote", "A quote picked locally each day"], ["clocks", "World clocks", "Jersey, New York, Tokyo and Sydney"],
    ["cluster", "Cluster status", "Ready nodes and running pods"], ["music", "Music", "Now playing, with play/pause, previous, next and loop"],
    ["links", "Quick links", "Shortcuts to dashboard pages"], ["mascot", "Little mascot", "A small companion for your desk"],
  ];
  const KNOWN = new Set(CATALOG.map((x) => x[0]));
  const quotes = [
    ["The secret of getting ahead is getting started.", "Mark Twain"],
    ["Great things are done by a series of small things brought together.", "Vincent van Gogh"],
    ["Simplicity boils down to two steps: identify the essential. Eliminate the rest.", "Leo Babauta"],
    ["It always seems impossible until it’s done.", "Nelson Mandela"],
    ["The best way out is always through.", "Robert Frost"],
    ["Well done is better than well said.", "Benjamin Franklin"],
    ["Nothing will work unless you do.", "Maya Angelou"],
  ];
  const mascotLines = ["You’ve got this. One thing at a time.", "Tiny break? Your shoulders called.", "I’m keeping an eye on the clock.", "Nice work showing up today.", "A little progress still counts."];
  const ADDED_V2 = ["player", "chat"];   // shown once to desks saved before these widgets existed
  const initial = { v: 2, widgets: ["player", "chat", "todo", "mascot", "links"], customizing: false, playerSource: "", chat: [], chatTarget: "", chatDraft: "", tasks: [], notes: "", location: "", weather: "", weatherError: "", countdown: null, calendarOffset: 0,
    focus: { mode: "focus", remaining: 1500, running: false, until: 0 }, mascot: 0 };
  const freshState = () => ({ ...initial, widgets: initial.widgets.slice(), tasks: [], chat: [], focus: { ...initial.focus } });
  function readState() {
    try {
      const saved = JSON.parse(store.get("desk.v1", "null"));
      if (!saved || typeof saved !== "object") return freshState();
      const widgets = Array.isArray(saved.widgets) ? [...new Set(saved.widgets.filter((x) => KNOWN.has(x)))] : initial.widgets.slice();
      if (!(saved.v >= 2)) ADDED_V2.forEach((id) => { if (!widgets.includes(id)) widgets.push(id); });
      const chat = Array.isArray(saved.chat) ? saved.chat.filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string").slice(-30)
        .map((m) => (m.pending ? { role: m.role, content: m.content || "(Interrupted before an answer arrived.)", error: !m.content } : { role: m.role, content: m.content, error: !!m.error })) : [];
      return { ...structuredClone(initial), ...saved, v: 2, widgets: widgets.slice(0, CATALOG.length), chat,
        playerSource: typeof saved.playerSource === "string" ? saved.playerSource : "",
        chatTarget: typeof saved.chatTarget === "string" ? saved.chatTarget : "", chatDraft: typeof saved.chatDraft === "string" ? saved.chatDraft : "",
        tasks: Array.isArray(saved.tasks) ? saved.tasks.filter((x) => x && typeof x.text === "string").slice(0, 100) : [],
        focus: { ...initial.focus, ...(saved.focus || {}) } };
    } catch (e) { return freshState(); }
  }
  const D = readState();
  const save = () => store.set("desk.v1", JSON.stringify(D, (k, v) => (k === "weatherBusy" ? undefined : v)));
  const title = (id) => (CATALOG.find((x) => x[0] === id) || [id, id])[1];
  const removeButton = (id) => D.customizing ? html`<span class="desk-move"><button type="button" data-desk="move" data-id="${id}" data-dir="-1" aria-label="Move ${title(id)} earlier" ${D.widgets.indexOf(id) === 0 ? "disabled" : ""}>‹</button><button type="button" data-desk="move" data-id="${id}" data-dir="1" aria-label="Move ${title(id)} later" ${D.widgets.indexOf(id) === D.widgets.length - 1 ? "disabled" : ""}>›</button></span><button class="desk-remove" type="button" data-desk="remove" data-id="${id}" aria-label="Remove ${title(id)}">×</button>` : "";
  function widget(id, body) { return html`<article class="desk-widget" data-widget="${id}" ${D.customizing ? raw('draggable="true"') : ""}><header><h2>${title(id)}</h2>${removeButton(id)}</header><div class="desk-widget-body">${body}</div></article>`; }
  function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
  function quoteToday() { const d = new Date(); const day = Math.floor(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000); return quotes[((day % quotes.length) + quotes.length) % quotes.length]; }
  function localDate(d) { return new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).format(d); }
  function calendar() {
    const now = new Date(), first = new Date(now.getFullYear(), now.getMonth() + D.calendarOffset, 1), offset = (first.getDay() + 6) % 7;
    const start = new Date(first.getFullYear(), first.getMonth(), 1 - offset);
    const cells = Array.from({ length: 42 }, (_, i) => { const d = new Date(start); d.setDate(start.getDate() + i); const isToday = d.toDateString() === now.toDateString(); return html`<span class="${d.getMonth() === first.getMonth() ? "" : "other"} ${isToday ? "today" : ""}">${d.getDate()}</span>`; });
    return html`<div class="desk-cal-head"><button type="button" class="btn small" data-desk="cal-prev" aria-label="Previous month">‹</button><b>${new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(first)}</b><button type="button" class="btn small" data-desk="cal-next" aria-label="Next month">›</button></div><div class="desk-calendar"><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span>${cells}</div>`;
  }
  function renderWidget(id) {
    if (id === "player") return widget(id, html`<div class="desk-player">
      <div class="desk-np"><div class="desk-np-art" data-player="art" aria-hidden="true">♫</div><div class="desk-np-meta"><b data-player="title">Nothing playing</b><span data-player="sub">Pick a list below to start</span></div></div>
      <div class="desk-seek"><span data-player="cur">0:00</span><input type="range" min="0" max="1000" step="1" value="0" data-desk-input="seek" aria-label="Seek"><span data-player="dur">0:00</span></div>
      <div class="desk-transport"><button type="button" data-desk="player-prev" aria-label="Previous song">⏮</button><button type="button" class="play" data-desk="player-toggle" aria-label="Play">▶</button><button type="button" data-desk="player-next" aria-label="Next song">⏭</button><button type="button" data-desk="player-shuffle" aria-pressed="false" aria-label="Shuffle">🔀</button><button type="button" data-desk="player-repeat" aria-label="Loop: off">🔁</button><button type="button" data-desk="player-sleep" aria-label="Sleep timer">⏾</button></div>
      <div class="desk-volume"><button type="button" data-desk="player-mute" aria-label="Mute">🔊</button><input type="range" min="0" max="1" step="0.05" value="1" data-desk-input="volume" aria-label="Volume"><span data-player="sleep"></span></div>
      <div class="desk-form"><select class="input" data-desk-input="player-source" aria-label="Play from"></select><button type="button" class="btn" data-desk="player-play-all">Play</button></div>
      <ul class="desk-player-list" data-player="list"></ul></div>`);
    if (id === "services") return widget(id, html`<div class="desk-svc" data-svc="body"><p class="desk-empty">Loading services…</p></div>`);
    if (id === "system") return widget(id, html`<div class="desk-sys" data-sys="body"></div>`);
    if (id === "chat") return widget(id, html`<div class="desk-chat">
      <div class="desk-form"><select class="input" data-desk-input="chat-target" aria-label="AI model"></select><button type="button" class="btn small" data-desk="chat-clear">Clear</button><button type="button" class="btn small" data-desk="ai">Open AI</button></div>
      <div class="desk-chat-log" data-chat="log" aria-live="polite"></div>
      <div class="desk-chat-status" data-chat="status"></div>
      <form class="desk-chat-form" data-desk-form="chat"><textarea class="input" data-desk-input="chat" rows="2" maxlength="4000" placeholder="Ask anything…" title="Enter sends · Shift+Enter adds a line" aria-label="Message for the AI">${D.chatDraft}</textarea><button class="btn primary" type="submit" data-chat="send">Send</button></form></div>`);
    if (id === "todo") return widget(id, html`<form class="desk-form" data-desk-form="todo"><input class="input" name="task" maxlength="120" placeholder="Add a task…" aria-label="New task"><button class="btn primary" type="submit">Add</button></form><ul class="desk-tasks">${D.tasks.length ? raw(D.tasks.map((t, i) => '<li class="' + (t.done ? 'done' : '') + '"><label><input type="checkbox" data-desk="task-toggle" data-index="' + i + '" ' + (t.done ? 'checked' : '') + '><span>' + esc(t.text) + '</span></label><button type="button" data-desk="task-delete" data-index="' + i + '" aria-label="Delete task">×</button></li>').join("")) : html`<li class="desk-empty">Nothing on the list yet.</li>`}</ul>`);
    if (id === "weather") return widget(id, html`<form class="desk-form" data-desk-form="weather"><input class="input" data-desk-input="location" maxlength="100" value="${D.location}" placeholder="Town or city (or Town, Country)" aria-label="Weather location"><button class="btn" type="submit" data-desk="weather-go">${D.weatherBusy ? "Loading…" : "Get weather"}</button></form><div class="desk-weather" data-weather>${weatherBody()}</div>`);
    if (id === "calendar") return widget(id, calendar());
    if (id === "notes") return widget(id, html`<textarea class="input desk-notes" data-desk-input="notes" maxlength="5000" placeholder="Jot something down…" aria-label="Desk notes">${D.notes}</textarea><div class="desk-note-status">Saved in this browser</div>`);
    if (id === "focus") return widget(id, html`<div class="desk-focus"><div class="desk-focus-time" data-focus-time>${timerText()}</div><div class="desk-muted" data-focus-mode>${D.focus.mode === "focus" ? "Focus" : "Break"}</div><div class="row"><button type="button" class="btn primary" data-desk="focus-toggle">${D.focus.running ? "Pause" : "Start"}</button><button type="button" class="btn" data-desk="focus-reset">Reset</button></div></div>`);
    if (id === "countdown") return widget(id, html`<form class="desk-countdown-form" data-desk-form="countdown"><input class="input" name="title" maxlength="60" placeholder="What are you counting down to?" value="${D.countdown ? D.countdown.title : ""}"><input class="input" name="date" type="date" value="${D.countdown ? D.countdown.date : ""}" aria-label="Countdown date"><button class="btn" type="submit">Save</button></form><div class="desk-countdown">${D.countdown && D.countdown.date ? countdownText() : "Pick a date to start a countdown."}</div>`);
    if (id === "quote") { const q = quoteToday(); return widget(id, html`<blockquote class="desk-quote">“${q[0]}”<cite>${q[1]}</cite></blockquote><span class="desk-muted">A different quote each day · picked on this device</span>`); }
    if (id === "clocks") return widget(id, html`<div class="desk-clocks">${[["Jersey", "Europe/Jersey"], ["New York", "America/New_York"], ["Tokyo", "Asia/Tokyo"], ["Sydney", "Australia/Sydney"]].map(([name, zone]) => html`<div><span>${name}</span><b data-zone="${zone}">--:--</b></div>`)}</div>`);
    if (id === "cluster") return widget(id, html`<div class="desk-cluster"><div><b data-cluster="nodes">—</b><span>ready nodes</span></div><div><b data-cluster="pods">—</b><span>running pods</span></div><div><b data-cluster="workloads">—</b><span>workloads</span></div></div><p class="desk-muted" data-cluster-note>${S.d ? "Live dashboard data" : "Waiting for cluster data"}</p>`);
    if (id === "music") return widget(id, html`<div class="desk-player desk-mini">
      <div class="desk-np"><div class="desk-np-art" data-mini="art" aria-hidden="true">♫</div><div class="desk-np-meta"><b data-mini="title">Nothing playing</b><span data-mini="sub">Press play to pick up where you left off</span></div></div>
      <div class="desk-transport"><button type="button" data-desk="player-prev" data-mini="prev" aria-label="Previous song">⏮</button><button type="button" class="play" data-desk="player-toggle" data-mini="toggle" aria-label="Play">▶</button><button type="button" data-desk="player-next" data-mini="next" aria-label="Next song">⏭</button><button type="button" data-desk="player-repeat" data-mini="repeat" aria-label="Loop: off">🔁</button></div>
      <div class="desk-mini-foot"><span data-mini="loop">Loop off</span><button type="button" class="btn small" data-desk="music">Open Music</button></div></div>`);
    if (id === "links") return widget(id, html`<div class="desk-links"><button type="button" class="btn" data-desk="overview">Overview</button><button type="button" class="btn" data-desk="music">Music</button><button type="button" class="btn" data-desk="ai">AI</button><button type="button" class="btn" data-desk="galaxy">AI galaxy</button><button type="button" class="btn" data-desk="settings">Settings</button></div>`);
    if (id === "mascot") { const n = ((D.mascot % mascotLines.length) + mascotLines.length) % mascotLines.length; return widget(id, html`<button type="button" class="desk-mascot" data-desk="mascot" aria-label="Say something encouraging"><span aria-hidden="true">${["🪴", "🦊", "🐸", "🐧", "🐱"][n]}</span><b>${mascotLines[n]}</b><small>Tap me</small></button>`); }
    return "";
  }
  function weatherBody() {
    const w = D.wx;
    if (D.weatherError) return html`<p class="desk-error">${D.weatherError}</p>`;
    if (!w || !w.now) return html`<p class="desk-empty">${D.weatherBusy ? "Loading the forecast…" : "Choose a place to see the weather. It refreshes itself every half hour."}</p>`;
    const deg = (v) => (v == null ? "–" : Math.round(v) + "°");
    const dayName = (d, i) => (i === 0 ? "Today" : new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(new Date(d + "T12:00:00")));
    const n = w.now;
    return html`<div class="desk-wx-now"><span class="desk-wx-icon" aria-hidden="true">${n.icon}</span><b>${deg(n.temp)}</b><div><div>${n.desc}</div>
        <small>Feels ${deg(n.feels)} · 💧 ${n.humidity == null ? "–" : Math.round(n.humidity) + "%"} · 💨 ${n.wind == null ? "–" : Math.round(n.wind) + " km/h"}</small></div></div>
      <div class="desk-wx-days">${(w.days || []).map((d, i) => html`<div><span>${dayName(d.date, i)}</span><b aria-hidden="true">${d.icon}</b><small>${deg(d.max)} / ${deg(d.min)}</small>${d.rain ? html`<small class="rain">${Math.round(d.rain * 10) / 10} mm</small>` : ""}</div>`)}</div>
      <p class="desk-muted">${w.place}${w.region ? ", " + w.region : ""} · updated ${new Date((w.fetched || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}${w.others && w.others.length ? html` · <span title="${"Also: " + w.others.join("; ")}">not this one? add the country</span>` : ""}</p>`;
  }
  function paintWeather() { const el = $('[data-widget="weather"] [data-weather]'); if (el) el.innerHTML = String(weatherBody()); const b = $('[data-widget="weather"] [data-desk="weather-go"]'); if (b) { b.textContent = D.weatherBusy ? "Loading…" : "Get weather"; b.disabled = !!D.weatherBusy; } }
  function timerText() { const left = D.focus.running ? Math.max(0, Math.ceil((D.focus.until - Date.now()) / 1000)) : Math.max(0, +D.focus.remaining || 0); return String(Math.floor(left / 60)).padStart(2, "0") + ":" + String(left % 60).padStart(2, "0"); }
  function countdownText() { const diff = Math.ceil((new Date(D.countdown.date + "T00:00:00") - new Date(new Date().toDateString())) / 86400000); return diff < 0 ? "That date has passed." : diff === 0 ? "Today! 🎉" : diff + " day" + (diff === 1 ? "" : "s") + " to go"; }
  function drawWidgets() {
    const host = $("#desk-widgets"); if (!host) return;
    host.innerHTML = D.widgets.map(renderWidget).join("") || '<div class="desk-no-widgets">Add a widget to make this desk yours.</div>';
    if (D.widgets.includes("player")) { if (!P.data || Date.now() - P.loadedAt > 60000) loadMusic(); drawPlayerList(); paintPlayer(); }
    if (D.widgets.includes("music")) paintMini();
    if (D.widgets.includes("chat")) { if (CH.targets === null) loadTargets(); paintChatTargets(); CH.stick = true; paintChat(); }
    if (D.widgets.includes("services")) { paintServices(); if (Date.now() - SV.at > 25000) loadServices(); }
    if (D.widgets.includes("system")) paintSystem();
  }
  function drawPicker() {
    const picker = $("#desk-picker"); if (!picker) return;
    picker.innerHTML = '<div class="desk-picker-head"><div><b>Add a widget</b><p>Choose any of the ' + CATALOG.length + ' desk widgets.</p></div><button class="btn icon-only" type="button" data-desk="picker-close" aria-label="Close">×</button></div><div class="desk-picker-grid">' + CATALOG.map(([id, name, sub]) => '<button type="button" class="desk-picker-item" data-desk="add" data-id="' + id + '" ' + (D.widgets.includes(id) ? 'disabled' : '') + '><b>' + esc(name) + '</b><span>' + esc(D.widgets.includes(id) ? "Already on your desk" : sub) + '</span></button>').join("") + '</div>';
  }
  function paintMode() { const page = $(".desk-page"); if (!page) return; page.classList.toggle("customizing", D.customizing); const b = $("[data-desk=customize]"); if (b) { b.setAttribute("aria-pressed", D.customizing ? "true" : "false"); b.textContent = D.customizing ? "Done" : "Arrange"; } }
  function tick() {
    if (S.view !== "desk") return;
    const now = new Date(), time = $("[data-desk-time]"), date = $("[data-desk-date]"), hello = $("[data-desk-greeting]");
    if (time) time.textContent = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(now);
    if (date) date.textContent = localDate(now);
    if (hello) hello.textContent = greeting();
    document.querySelectorAll("[data-zone]").forEach((el) => { try { el.textContent = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", timeZone: el.dataset.zone }).format(now); } catch (e) { el.textContent = "--:--"; } });
    const focus = $(`[data-widget="focus"]`);
    if (focus) {
      const el = focus.querySelector("[data-focus-time]"); if (el) el.textContent = timerText();
      if (D.focus.running && Date.now() >= D.focus.until) { D.focus.running = false; D.focus.mode = D.focus.mode === "focus" ? "break" : "focus"; D.focus.remaining = D.focus.mode === "focus" ? 1500 : 300; save(); const mode = focus.querySelector("[data-focus-mode]"), button = focus.querySelector("[data-desk=focus-toggle]"); if (mode) mode.textContent = D.focus.mode === "focus" ? "Focus" : "Break"; if (button) button.textContent = "Start"; toast(D.focus.mode === "break" ? "Focus block complete. Time for a break." : "Break complete. Ready for another focus block?"); }
    }
    updateCluster();
    if (CH.busy) paintChatStatus();
    if (now.getSeconds() % 5 === 0) paintSystem();
    if (D.widgets.includes("services") && Date.now() - SV.at > 30000 && !document.hidden) loadServices();
    if (now.getSeconds() === 30 && weatherStale() && !document.hidden) getWeather(true);
  }
  function updateCluster() {
    const el = $('[data-widget="cluster"]'); if (!el) return;
    const nodes = el.querySelector('[data-cluster="nodes"]'), pods = el.querySelector('[data-cluster="pods"]'), workloads = el.querySelector('[data-cluster="workloads"]'), note = el.querySelector("[data-cluster-note]");
    if (!S.d) { if (note) note.textContent = "Waiting for cluster data"; return; }
    if (nodes) nodes.textContent = S.d.nodes.filter((n) => n.ready).length + "/" + S.d.nodes.length;
    if (pods) pods.textContent = S.d.pods.filter((p) => p.status === "Running").length;
    if (workloads) workloads.textContent = S.d.workloads.length;
    if (note) note.textContent = S.res && S.res.updated ? "Updated " + new Date(S.res.updated * 1000).toLocaleTimeString() : "Live dashboard data";
  }
  function openPicker() { drawPicker(); $("#desk-picker").hidden = false; $("#desk-picker-backdrop").hidden = false; $("#desk-picker").querySelector("[data-desk=picker-close]").focus(); }
  function closePicker() { const p = $("#desk-picker"); if (p) p.hidden = true; const backdrop = $("#desk-picker-backdrop"); if (backdrop) backdrop.hidden = true; }
  async function getWeather(quiet) {
    const loc = (D.location || "").trim();
    if (!loc) { if (!quiet) { toast("Enter a town or city first."); $("[data-desk-input=location]")?.focus(); } return; }
    if (D.weatherBusy) return;
    D.weatherBusy = true; if (!quiet) D.weatherError = ""; paintWeather();
    try {
      const r = await getJSON("/api/homelab/weather?location=" + encodeURIComponent(loc));
      if (!r.ok) throw new Error(r.error || "Weather is unavailable.");
      D.wx = r; D.weatherError = ""; D.weather = "";
    } catch (e) { if (!quiet || !D.wx) D.weatherError = e.message || "Couldn't load the weather."; }
    D.weatherBusy = false; save(); paintWeather();
  }
  // the forecast keeps itself fresh while the Desk is open (and when it opens with an old one)
  const weatherStale = () => D.widgets.includes("weather") && D.location && !D.weatherBusy && (!D.wx || Date.now() / 1000 - (D.wx.fetched || 0) > 1800);
  // ---- music player: drives the dashboard's one shared player (music.js), so playback carries on across pages ----
  const P = { data: null, loading: false, error: "", loadedAt: 0, tracks: null, tracksFor: "" };
  const fmtTime = (s) => (isFinite(s) && s > 0 ? Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0") : "0:00");
  async function loadMusic() {
    if (P.loading) return;
    P.loading = true; P.error = "";
    try {
      const r = await getJSON("/api/music");
      if (!r.ok) throw new Error(r.error || "Music is unavailable.");
      P.data = r; P.loadedAt = Date.now(); P.tracks = null; P.tracksFor = "";
    } catch (e) { P.error = e.message || "Couldn't load your music."; }
    P.loading = false; drawPlayerList();
  }
  function playerSources() {
    const d = P.data || {}, likes = (d.sc && d.sc.likes) || [], lib = d.tracks || [];
    return [["likes", "Liked songs", likes.length], ["library", "Library", lib.length]]
      .concat((d.playlists || []).map((p) => ["pl:" + p.id, p.name || "Playlist", p.count || 0]));
  }
  function currentSource() {
    const sources = playerSources();
    if (sources.some((x) => x[0] === D.playerSource)) return D.playerSource;
    return sources[0][2] || !sources[1][2] ? "likes" : "library";
  }
  async function loadPlayerTracks() {
    const id = currentSource(), d = P.data; if (!d) return;
    if (P.tracksFor === id && P.tracks) return;
    P.tracksFor = id; P.tracks = null;
    let tracks = [];
    if (id === "likes") tracks = (d.sc && d.sc.likes) || [];
    else if (id === "library") tracks = d.tracks || [];
    else {
      drawPlayerList();
      try { const r = await getJSON("/api/music/playlists?id=" + encodeURIComponent(id.slice(3))); tracks = r.ok && r.playlist && Array.isArray(r.playlist.tracks) ? r.playlist.tracks : []; }
      catch (e) { tracks = []; }
    }
    if (P.tracksFor !== id) return;
    P.tracks = tracks; drawPlayerList();
  }
  function drawPlayerList() {
    const w = $('[data-widget="player"]'); if (!w) return;
    const select = w.querySelector('[data-desk-input="player-source"]'), list = w.querySelector('[data-player="list"]'), play = w.querySelector('[data-desk="player-play-all"]');
    if (!P.data) {
      select.innerHTML = '<option value="">' + (P.error ? "Music unavailable" : "Loading…") + "</option>"; select.disabled = true; play.disabled = true;
      list.innerHTML = P.error ? '<li class="desk-error">' + esc(P.error) + ' <button type="button" class="btn small" data-desk="player-retry">Retry</button></li>' : '<li class="desk-empty">Loading your music…</li>';
      return;
    }
    const id = currentSource();
    select.disabled = false;
    select.innerHTML = playerSources().map(([value, name, count]) => '<option value="' + esc(value) + '"' + (value === id ? " selected" : "") + ">" + esc(name + " (" + count + ")") + "</option>").join("");
    if (P.tracksFor !== id || !P.tracks) { play.disabled = true; list.innerHTML = '<li class="desk-empty">Loading songs…</li>'; loadPlayerTracks(); return; }
    play.disabled = !P.tracks.length;
    const shown = P.tracks.slice(0, 200);
    list.innerHTML = shown.length ? shown.map((t, i) => {
      const d = NY.music ? NY.music.describe(t) : { key: "", title: t.title || t.name || "Untitled", sub: "" };
      return '<li data-key="' + esc(d.key) + '"><button type="button" data-desk="player-track" data-index="' + i + '"><b>' + esc(d.title) + "</b><span>" + esc(d.sub) + "</span></button></li>";
    }).join("") + (P.tracks.length > shown.length ? '<li class="desk-empty">Showing the first 200 of ' + P.tracks.length + " songs. Play starts the whole list.</li>" : "")
      : '<li class="desk-empty">' + (id === "likes" ? "No liked songs yet. Like songs in Music to see them here." : id === "library" ? "Your library is empty. Choose a music folder in Music › Settings." : "This playlist is empty.") + "</li>";
    paintPlayer();
  }
  function paintPlayer() {
    const w = $('[data-widget="player"]'); if (!w) return;
    const q = (name) => w.querySelector('[data-player="' + name + '"]');
    if (!NY.music) { q("sub").textContent = "The music player isn't loaded."; return; }
    const st = NY.music.state(), t = st.track;
    q("title").textContent = st.live ? "Server audio" : t ? t.title : "Nothing playing";
    q("sub").textContent = st.live ? "Live from the host" : t ? t.sub : "Pick a list below to start";
    const art = q("art"), want = (t && t.art) || "";
    if (art.dataset.src !== want) { art.dataset.src = want; art.innerHTML = "♫" + (want ? '<img alt="" src="' + esc(want) + '">' : ""); }
    q("cur").textContent = st.live ? "" : fmtTime(st.current);
    q("dur").textContent = st.live ? "live" : fmtTime(st.duration);
    const seek = w.querySelector('[data-desk-input="seek"]');
    seek.disabled = !st.seekable || !st.duration;
    if (!seek.matches(":active")) seek.value = st.duration ? Math.round((1000 * st.current) / st.duration) : 0;
    const toggle = w.querySelector('[data-desk="player-toggle"]');
    toggle.textContent = st.playing ? "⏸" : "▶"; toggle.setAttribute("aria-label", st.playing ? "Pause" : "Play");
    w.querySelector('[data-desk="player-prev"]').disabled = !st.hasPrev;
    w.querySelector('[data-desk="player-next"]').disabled = !st.hasNext;
    paintRepeat(w.querySelector('[data-desk="player-repeat"]'), st);
    const shuffle = w.querySelector('[data-desk="player-shuffle"]');
    shuffle.classList.toggle("on", st.shuffle); shuffle.setAttribute("aria-pressed", st.shuffle ? "true" : "false"); shuffle.title = st.shuffle ? "Shuffle on" : "Shuffle off";
    w.querySelectorAll(".desk-player-list li[data-key]").forEach((li) => li.classList.toggle("on", !!t && li.dataset.key === t.key));
    const vol = w.querySelector('[data-desk-input="volume"]'), mute = w.querySelector('[data-desk="player-mute"]'), quiet = st.muted || !st.volume;
    if (!vol.matches(":active")) vol.value = st.volume;
    mute.textContent = quiet ? "🔇" : st.volume < 0.5 ? "🔉" : "🔊"; mute.setAttribute("aria-label", quiet ? "Unmute" : "Mute");
    const sleep = w.querySelector('[data-desk="player-sleep"]');
    sleep.classList.toggle("on", !!st.sleep); sleep.title = "Sleep timer: " + (st.sleep || "off");
    q("sleep").textContent = st.sleep ? "Sleep · " + st.sleep : "";
  }
  const LOOP_TEXT = { off: "Loop off", one: "Looping this song", all: "Looping the playlist" };
  function paintRepeat(btn, st) {
    if (!btn) return;
    btn.textContent = st.repeat === "one" ? "🔂" : "🔁";
    btn.classList.toggle("on", st.repeat !== "off");
    const label = "Loop: " + (st.repeat === "one" ? "song" : st.repeat === "all" ? "playlist" : "off") + " (click to change)";
    btn.title = label; btn.setAttribute("aria-label", label); btn.setAttribute("aria-pressed", st.repeat === "off" ? "false" : "true");
  }
  // The small Music widget: what's playing and the basic controls (the Music player widget has the lists).
  function paintMini() {
    const w = $('[data-widget="music"]'); if (!w || !NY.music) return;
    const q = (name) => w.querySelector('[data-mini="' + name + '"]');
    const st = NY.music.state(), t = st.track;
    q("title").textContent = st.live ? "Server audio" : t ? t.title : "Nothing playing";
    q("sub").textContent = st.live ? "Live from the host" : t ? t.sub + (st.duration ? " · " + fmtTime(st.current) + " / " + fmtTime(st.duration) : "") : "Press play to pick up where you left off";
    const art = q("art"), want = (t && t.art) || "";
    if (art.dataset.src !== want) { art.dataset.src = want; art.innerHTML = "♫" + (want ? '<img alt="" src="' + esc(want) + '">' : ""); }
    const toggle = q("toggle");
    toggle.textContent = st.playing ? "⏸" : "▶"; toggle.setAttribute("aria-label", st.playing ? "Pause" : "Play");
    q("prev").disabled = !st.hasPrev; q("next").disabled = !st.hasNext;
    paintRepeat(q("repeat"), st);
    q("loop").textContent = LOOP_TEXT[st.repeat] || "";
  }
  // Play with nothing loaded: the last saved queue, else liked songs, else the library.
  async function startOrToggle(btn) {
    if (!NY.music) return;
    if (NY.music.toggle()) return;
    if (btn) btn.disabled = true;
    try {
      if (await NY.music.resume()) return;
      if (!P.data) await loadMusic();
      const d = P.data || {}, likes = (d.sc && d.sc.likes) || [], list = likes.length ? likes : d.tracks || [];
      if (list.length) NY.music.playQueue(list, NY.music.state().shuffle ? Math.floor(Math.random() * list.length) : 0);
      else toast(P.error || "There's nothing to play yet. Add music or like songs in Music.");
    } finally { if (btn) btn.disabled = false; paintMini(); paintPlayer(); }
  }
  function playFromList(i) {
    if (!NY.music || !P.tracks || !P.tracks.length) return;
    const start = i == null ? (NY.music.state().shuffle ? Math.floor(Math.random() * P.tracks.length) : 0) : i;
    NY.music.playQueue(P.tracks, start);
  }
  document.addEventListener("ny-music", () => { if (S.view === "desk") { paintPlayer(); paintMini(); } });
  document.addEventListener("error", (e) => { if (e.target && e.target.matches && e.target.matches(".desk-np-art img")) e.target.remove(); }, true);

  // ---- services (the Services page's monitor) and system load ----
  const SV = { list: null, error: "", at: 0, loading: false };
  async function loadServices() {
    if (SV.loading) return;
    SV.loading = true; SV.at = Date.now();
    try { const r = await getJSON("/api/homelab/services"); if (!r.ok) throw new Error(r.error || "The service monitor is off."); SV.list = r.services || []; SV.error = ""; }
    catch (e) { SV.error = e.message || "Couldn't load services."; }
    SV.loading = false; paintServices();
  }
  function paintServices() {
    const el = $('[data-widget="services"] [data-svc="body"]'); if (!el) return;
    if (SV.list === null) { el.innerHTML = SV.error ? '<p class="desk-error">' + esc(SV.error) + "</p>" : '<p class="desk-empty">Loading services…</p>'; return; }
    if (!SV.list.length) { el.innerHTML = '<p class="desk-empty">No services watched yet.</p><button type="button" class="btn" data-desk="services">Add services</button>'; return; }
    const order = { down: 0, degraded: 1, pending: 2, up: 3, paused: 4 };
    const up = SV.list.filter((s) => s.status === "up").length, active = SV.list.filter((s) => s.status !== "paused").length, bad = active - up;
    const rows = SV.list.slice().sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name)).slice(0, 7);
    el.innerHTML = '<div class="desk-svc-sum ' + (bad ? "bad" : "good") + '"><b>' + up + "/" + active + "</b><span>" + (bad ? bad + " need" + (bad === 1 ? "s" : "") + " attention" : "all up") + "</span></div>" +
      '<ul class="desk-svc-list">' + rows.map((s) => '<li><i class="desk-dot ' + esc(s.status) + '"></i><span>' + esc(s.name) + "</span><small>" +
        esc(s.status === "up" && s.last && s.last.ms != null ? Math.round(s.last.ms) + " ms" : s.status === "paused" ? "paused" : s.status === "pending" ? "checking" : "down") + "</small></li>").join("") + "</ul>" +
      (SV.list.length > rows.length ? '<button type="button" class="btn small" data-desk="services">All ' + SV.list.length + " services</button>" : "");
  }
  function paintSystem() {
    const el = $('[data-widget="system"] [data-sys="body"]'); if (!el) return;
    if (!S.d) { el.innerHTML = '<p class="desk-empty">Waiting for cluster data…</p>'; return; }
    const ns = S.d.nodes, sum = (k) => ns.reduce((a, n) => a + (+n[k] || 0), 0);
    const pct = (used, total) => (total ? Math.round((100 * used) / total) : null);
    const temps = ns.map((n) => (n.hw || {}).temp_c).filter((x) => x != null);
    const rows = [["CPU", pct(sum("cpu_used"), sum("cpu_cores"))], ["Memory", pct(sum("mem_used"), sum("mem_total"))], ["Disk", pct(sum("disk_used"), sum("disk_total"))]];
    el.innerHTML = rows.map(([name, v]) => '<div class="desk-sys-row"><span>' + name + '</span><div class="desk-sys-bar"><i class="' + (v >= 90 ? "bad" : v >= 75 ? "warn" : "") + '" style="width:' + (v == null ? 0 : Math.min(100, v)) + '%"></i></div><b>' + (v == null ? "–" : v + "%") + "</b></div>").join("") +
      '<p class="desk-muted">' + ns.filter((n) => n.ready).length + "/" + ns.length + " nodes ready" + (temps.length ? " · hottest " + Math.round(Math.max(...temps)) + " °C" : "") + "</p>";
  }

  // ---- AI chat: streams from the same /api/ai/chat the AI page uses; the conversation is kept in this browser ----
  const CH = { busy: false, ctrl: null, sid: "", wait: "", started: 0, targets: null, targetError: "", loadingTargets: false, stick: false, raf: 0 };
  const CHAT_SYSTEM = "You are Nodeyard AI, answering in a small chat panel on the user's dashboard desk. Be brief, friendly and clear. " +
    "Use plain text, short paragraphs and short lists. Put any code in fenced code blocks.";
  async function loadTargets() {
    if (CH.loadingTargets) return;
    CH.loadingTargets = true;
    try {
      const r = await getJSON("/api/ai/targets");
      CH.targets = r.ok !== false && Array.isArray(r.targets) ? r.targets : []; CH.targetError = r.ok === false ? r.error || "Couldn't load the AI models." : "";
    } catch (e) { CH.targets = []; CH.targetError = e.message || "Couldn't load the AI models."; }
    CH.loadingTargets = false;
    const ready = CH.targets.filter((x) => x.ready);
    if (!ready.some((x) => x.id === D.chatTarget)) { const pick = ready.find((x) => x.id === "split") || ready[0]; D.chatTarget = pick ? pick.id : ""; save(); }
    paintChatTargets(); paintChatStatus();
  }
  function paintChatTargets() {
    const select = $('[data-widget="chat"] [data-desk-input="chat-target"]'); if (!select) return;
    const ready = (CH.targets || []).filter((x) => x.ready);
    select.disabled = !ready.length;
    select.innerHTML = ready.length
      ? ready.map((x) => '<option value="' + esc(x.id) + '"' + (x.id === D.chatTarget ? " selected" : "") + ">" + esc((x.name || x.id) + (x.kind === "ollama" ? " · " + (x.detail || "Ollama") : " · split model")) + "</option>").join("")
      : '<option value="">' + (CH.targets === null ? "Loading models…" : "No model is ready") + "</option>";
  }
  const visibleText = (text) => String(text || "").replace(/<think>[\s\S]*?(<\/think>|$)/g, "").replace(/<\/?think>/g, "").trim();
  function chatHTML(text) {
    return visibleText(text).split("```").map((part, i) => (i % 2 ? "<pre><code>" + esc(part.replace(/^[\w+#.-]*\n/, "")) + "</code></pre>" : esc(part).replace(/\n/g, "<br>"))).join("");
  }
  function paintChat() {
    CH.raf = 0;
    const log = $('[data-widget="chat"] [data-chat="log"]'); if (!log) return;
    const near = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    log.innerHTML = D.chat.length
      ? D.chat.map((m, i) => '<div class="desk-msg ' + m.role + (m.error ? " error" : "") + '">' + (visibleText(m.content) ? chatHTML(m.content) : '<span class="desk-typing" aria-label="Waiting for the answer"><i></i><i></i><i></i></span>') +
          (m.role === "assistant" && !m.pending && visibleText(m.content) ? '<button type="button" class="desk-copy" data-desk="chat-copy" data-index="' + i + '" aria-label="Copy this answer" title="Copy">⧉</button>' : "") + "</div>").join("")
      : '<p class="desk-empty">Ask a question. Answers stream in here, and your music keeps playing.</p>';
    if (near || CH.stick) log.scrollTop = log.scrollHeight;
    CH.stick = false;
    paintChatStatus();
  }
  function paintChatSoon() { if (!CH.raf) CH.raf = requestAnimationFrame(paintChat); }
  function paintChatStatus() {
    const w = $('[data-widget="chat"]'); if (!w) return;
    const status = w.querySelector('[data-chat="status"]'), send = w.querySelector('[data-chat="send"]');
    const ready = (CH.targets || []).some((x) => x.ready);
    if (CH.busy) status.textContent = (CH.wait || "Writing…") + " · " + Math.floor((Date.now() - CH.started) / 1000) + "s";
    else if (CH.targets !== null && !ready) status.textContent = CH.targetError || "No AI model is ready. Start one in AI › Models.";
    else status.textContent = "";
    status.classList.toggle("desk-error", !CH.busy && CH.targets !== null && !ready);
    send.textContent = CH.busy ? "Stop" : "Send"; send.classList.toggle("danger", CH.busy); send.classList.toggle("primary", !CH.busy);
  }
  function stopChat() {
    if (CH.sid) postJSON("/api/ai/stop", { id: CH.sid }).catch(() => {});
    if (CH.ctrl) CH.ctrl.abort();
  }
  async function sendChat() {
    if (CH.busy) { stopChat(); return; }
    const box = $('[data-desk-input="chat"]'), text = String(box ? box.value : D.chatDraft || "").trim();
    if (!text) { if (box) box.focus(); return; }
    if (!D.chatTarget) { toast("No AI model is ready. Start one in AI › Models."); loadTargets(); return; }
    const reply = { role: "assistant", content: "", pending: true };
    D.chat.push({ role: "user", content: text.slice(0, 4000) }, reply); D.chat = D.chat.slice(-30);
    D.chatDraft = ""; if (box) box.value = ""; save();
    const history = D.chat.filter((m) => m !== reply && !m.error && visibleText(m.content)).slice(-8).map((m) => ({ role: m.role, content: visibleText(m.content) }));
    CH.busy = true; CH.ctrl = new AbortController(); CH.sid = Date.now().toString(36) + Math.random().toString(36).slice(2);
    CH.wait = "Sending…"; CH.started = Date.now(); CH.stick = true; paintChat();
    let finished = false, reason = "";
    try {
      const r = await fetch("/api/ai/chat", { method: "POST", cache: "no-store", signal: CH.ctrl.signal, headers: { "Content-Type": "application/json", "X-Nodeyard": "1" },
        body: JSON.stringify({ target: D.chatTarget, messages: [{ role: "system", content: CHAT_SYSTEM }].concat(history), temperature: 0.3, max_tokens: 900, stream_id: CH.sid }) });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) { let j = {}; try { j = await r.json(); } catch (e) { /* not JSON */ } throw new Error(j.error || "The model didn't answer (HTTP " + r.status + ")."); }
      CH.wait = "Thinking…"; paintChatStatus();
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true }); let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") { finished = true; continue; }
          let j; try { j = JSON.parse(data); } catch (e) { continue; }
          if (j.error) throw new Error(j.error.message || j.error);
          if (j.prompt_progress && !reply.content) { const pp = j.prompt_progress; CH.wait = "Reading your message… " + Math.round((100 * (pp.processed || 0)) / Math.max(1, pp.total || 1)) + "%"; paintChatStatus(); }
          const choice = j.choices && j.choices[0];
          if (!choice) continue;
          if (choice.finish_reason) { finished = true; reason = choice.finish_reason; }
          const delta = choice.delta || {};
          if (delta.content) { reply.content += delta.content; CH.wait = "Writing…"; paintChatSoon(); }
          else if (delta.reasoning_content && !reply.content) { CH.wait = "Thinking…"; paintChatStatus(); }
        }
      }
      if (!finished && !visibleText(reply.content)) throw new Error("The model closed the connection without answering. Try again.");
      if (!finished) reply.content += "\n\n⚠ The answer was cut off.";
      else if (reason === "length") reply.content += "\n\n(That's the longest answer this panel asks for. Open AI for longer replies.)";
      reply.content = visibleText(reply.content);
    } catch (e) {
      if (e.name === "AbortError") reply.content = visibleText(reply.content) ? visibleText(reply.content) + "\n\n(Stopped.)" : "(Stopped.)";
      else { reply.error = true; reply.content = (visibleText(reply.content) ? visibleText(reply.content) + "\n\n" : "") + "⚠ " + (e.message || "Something went wrong."); }
    } finally {
      delete reply.pending; CH.busy = false; CH.ctrl = null; CH.sid = ""; CH.wait = "";
      save(); paintChat();
    }
  }

  function click(e) {
    const b = e.target.closest("[data-desk]"); if (!b) return;
    const action = b.dataset.desk;
    if (action === "add") { const id = b.dataset.id; if (KNOWN.has(id) && !D.widgets.includes(id)) { D.widgets.push(id); save(); drawWidgets(); drawPicker(); paintMode(); } }
    else if (action === "remove") { D.widgets = D.widgets.filter((x) => x !== b.dataset.id); save(); drawWidgets(); paintMode(); }
    else if (action === "picker-close") closePicker();
    else if (action === "customize") { D.customizing = !D.customizing; save(); drawWidgets(); paintMode(); }
    else if (action === "add-widget") openPicker();
    else if (action === "task-delete") { D.tasks.splice(+b.dataset.index, 1); save(); drawWidgets(); paintMode(); }
    else if (action === "weather") getWeather(false);
    else if (action === "cal-prev" || action === "cal-next") { D.calendarOffset += action === "cal-prev" ? -1 : 1; save(); drawWidgets(); paintMode(); }
    else if (action === "focus-toggle") { if (D.focus.running) { D.focus.remaining = Math.max(0, Math.ceil((D.focus.until - Date.now()) / 1000)); D.focus.running = false; } else { D.focus.until = Date.now() + Math.max(1, D.focus.remaining) * 1000; D.focus.running = true; } save(); drawWidgets(); paintMode(); }
    else if (action === "focus-reset") { D.focus = { mode: "focus", remaining: 1500, running: false, until: 0 }; save(); drawWidgets(); paintMode(); }
    else if (action === "mascot") { D.mascot++; save(); drawWidgets(); paintMode(); }
    else if (action === "player-toggle") startOrToggle(b);
    else if (action === "player-repeat") { if (NY.music) NY.music.cycleRepeat(); }
    else if (action === "player-prev") { if (NY.music) NY.music.prev(); }
    else if (action === "player-next") { if (NY.music) NY.music.next(); }
    else if (action === "player-shuffle") { if (NY.music) NY.music.setShuffle(!NY.music.state().shuffle); }
    else if (action === "player-play-all") playFromList(null);
    else if (action === "player-track") playFromList(+b.dataset.index);
    else if (action === "player-retry") loadMusic();
    else if (action === "player-mute") { if (NY.music) NY.music.toggleMute(); }
    else if (action === "player-sleep") { if (NY.music) NY.music.cycleSleep(); }
    else if (action === "services") go("services");
    else if (action === "move") { const i = D.widgets.indexOf(b.dataset.id), j = i + (+b.dataset.dir); if (i >= 0 && j >= 0 && j < D.widgets.length) { [D.widgets[i], D.widgets[j]] = [D.widgets[j], D.widgets[i]]; save(); drawWidgets(); paintMode(); $(`[data-widget="${b.dataset.id}"] [data-desk="move"][data-dir="${b.dataset.dir}"]`)?.focus(); } }
    else if (action === "chat-copy") { const m = D.chat[+b.dataset.index]; if (m) U.copyText(visibleText(m.content)); }
    else if (action === "chat-clear") { if (CH.busy) stopChat(); D.chat = []; save(); paintChat(); $('[data-desk-input="chat"]')?.focus(); }
    else if (action === "music") go("music");
    else if (["overview", "ai", "settings", "galaxy"].includes(action)) go(action);
  }
  function submit(e) {
    const form = e.target.closest("form[data-desk-form]"); if (!form) return;
    e.preventDefault(); const data = new FormData(form);
    if (form.dataset.deskForm === "todo") { const text = String(data.get("task") || "").trim(); if (!text) return; D.tasks.push({ text, done: false }); D.tasks = D.tasks.slice(-100); save(); drawWidgets(); paintMode(); $("[data-widget=todo] input[name=task]")?.focus(); }
    else if (form.dataset.deskForm === "chat") sendChat();
    else if (form.dataset.deskForm === "weather") { D.location = String(data.get("location") || $('[data-desk-input="location"]')?.value || "").trim(); getWeather(false); }
    else if (form.dataset.deskForm === "countdown") { const date = String(data.get("date") || ""), titleText = String(data.get("title") || "").trim(); D.countdown = date ? { date, title: titleText || "Countdown" } : null; save(); drawWidgets(); paintMode(); }
  }
  function change(e) {
    const el = e.target;
    if (el.matches('[data-desk="task-toggle"]')) { const item = D.tasks[+el.dataset.index]; if (item) { item.done = el.checked; save(); el.closest("li").classList.toggle("done", el.checked); } }
    else if (el.matches('[data-desk-input="player-source"]')) { D.playerSource = el.value; save(); drawPlayerList(); }
    else if (el.matches('[data-desk-input="chat-target"]')) { D.chatTarget = el.value; save(); }
  }
  function input(e) {
    const el = e.target;
    if (el.matches('[data-desk-input="notes"]')) { D.notes = el.value; save(); }
    else if (el.matches('[data-desk-input="location"]')) { D.location = el.value; save(); }
    else if (el.matches('[data-desk-input="chat"]')) { D.chatDraft = el.value; save(); }
    else if (el.matches('[data-desk-input="seek"]')) { if (NY.music) NY.music.seek(+el.value / 1000); }
    else if (el.matches('[data-desk-input="volume"]')) { if (NY.music) NY.music.setVolume(+el.value); }
  }
  function keydown(e) {
    if (e.key === "Escape" && $("#desk-picker") && !$("#desk-picker").hidden) closePicker();
    else if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.target.matches && e.target.matches('[data-desk-input="chat"]')) { e.preventDefault(); sendChat(); }
  }
  // Arrange mode: drag a widget onto another to put it there.
  let dragging = "";
  document.addEventListener("dragstart", (e) => { const w = e.target.closest && e.target.closest(".desk-page.customizing .desk-widget"); if (!w) return; dragging = w.dataset.widget; w.classList.add("dragging"); try { e.dataTransfer.setData("text/plain", dragging); e.dataTransfer.effectAllowed = "move"; } catch (err) { /* old browsers */ } });
  document.addEventListener("dragover", (e) => { if (!dragging) return; const w = e.target.closest && e.target.closest(".desk-widget"); if (!w) return; e.preventDefault(); document.querySelectorAll(".desk-widget.drop").forEach((x) => x !== w && x.classList.remove("drop")); if (w.dataset.widget !== dragging) w.classList.add("drop"); });
  document.addEventListener("drop", (e) => {
    if (!dragging) return; const w = e.target.closest && e.target.closest(".desk-widget"); e.preventDefault();
    if (w && w.dataset.widget !== dragging) { const from = D.widgets.indexOf(dragging), to = D.widgets.indexOf(w.dataset.widget); if (from >= 0 && to >= 0) { D.widgets.splice(to, 0, D.widgets.splice(from, 1)[0]); save(); drawWidgets(); paintMode(); } }
  });
  document.addEventListener("dragend", () => { dragging = ""; document.querySelectorAll(".desk-widget.dragging,.desk-widget.drop").forEach((x) => x.classList.remove("dragging", "drop")); });
  document.addEventListener("click", click);
  document.addEventListener("submit", submit);
  document.addEventListener("change", change);
  document.addEventListener("input", input);
  document.addEventListener("keydown", keydown);

  V.desk = {
    build() {
      NY.freshHTML($("#view"), html`<main class="desk-page"><header class="desk-top"><a class="desk-brand" href="#overview">nodeyard <span>desk</span></a><div class="desk-actions"><button type="button" class="btn" data-desk="customize" aria-pressed="${D.customizing}">Arrange</button><button type="button" class="btn primary" data-desk="add-widget">＋ Add widgets</button><button type="button" class="btn" data-desk="overview">Dashboard</button></div></header><section class="desk-hero"><div class="desk-greeting" data-desk-greeting></div><time class="desk-clock" data-desk-time></time><div class="desk-date" data-desk-date></div></section><section class="desk-widgets" id="desk-widgets" aria-label="Desk widgets"></section><div class="desk-foot">Your selected dashboard wallpaper · widgets save in this browser</div><button id="desk-picker-backdrop" class="desk-picker-backdrop" type="button" data-desk="picker-close" aria-label="Close widget picker" hidden></button><section class="desk-picker" id="desk-picker" role="dialog" aria-modal="true" aria-label="Add a widget" hidden></section></main>`);
      CH.targets = (CH.targets || []).some((x) => x.ready) ? CH.targets : null;   // look again if no model was ready last time
      drawWidgets(); paintMode(); tick();
      if (weatherStale()) getWeather(true);
      if (NY.music && (D.widgets.includes("player") || D.widgets.includes("music"))) NY.music.restore();
    },
    update() { tick(); },
  };
  setInterval(tick, 1000);
})();

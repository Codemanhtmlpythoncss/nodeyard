// The full-screen wallpaper clock and personal widgets.
(function () {
  "use strict";
  const { esc, html, raw } = NY;
  const U = NY.ui;
  const { S, V, $, go, postJSON, toast, store } = U;

  const CATALOG = [
    ["todo", "To-do list", "A small list for today"], ["weather", "Weather", "Conditions for a place you choose"],
    ["calendar", "Calendar", "A month at a glance"], ["notes", "Notes", "A saved scratchpad"],
    ["focus", "Focus timer", "A simple focus and break timer"], ["countdown", "Countdown", "Count down to a date"],
    ["quote", "Daily quote", "A quote picked locally each day"], ["clocks", "World clocks", "Jersey, New York, Tokyo and Sydney"],
    ["cluster", "Cluster status", "Ready nodes and running pods"], ["music", "Music", "Jump back into your music"],
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
  const initial = { widgets: ["todo", "mascot", "links"], customizing: false, tasks: [], notes: "", location: "", weather: "", weatherError: "", countdown: null, calendarOffset: 0,
    focus: { mode: "focus", remaining: 1500, running: false, until: 0 }, mascot: 0 };
  const freshState = () => ({ ...initial, widgets: initial.widgets.slice(), tasks: [], focus: { ...initial.focus } });
  function readState() {
    try {
      const saved = JSON.parse(store.get("desk.v1", "null"));
      if (!saved || typeof saved !== "object") return freshState();
      return { ...structuredClone(initial), ...saved,
        widgets: Array.isArray(saved.widgets) ? [...new Set(saved.widgets.filter((x) => KNOWN.has(x)))].slice(0, CATALOG.length) : initial.widgets.slice(),
        tasks: Array.isArray(saved.tasks) ? saved.tasks.filter((x) => x && typeof x.text === "string").slice(0, 100) : [],
        focus: { ...initial.focus, ...(saved.focus || {}) } };
    } catch (e) { return freshState(); }
  }
  const D = readState();
  const save = () => store.set("desk.v1", JSON.stringify(D));
  const title = (id) => (CATALOG.find((x) => x[0] === id) || [id, id])[1];
  const removeButton = (id) => D.customizing ? html`<button class="desk-remove" type="button" data-desk="remove" data-id="${id}" aria-label="Remove ${title(id)}">×</button>` : "";
  function widget(id, body) { return html`<article class="desk-widget" data-widget="${id}"><header><h2>${title(id)}</h2>${removeButton(id)}</header><div class="desk-widget-body">${body}</div></article>`; }
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
    if (id === "todo") return widget(id, html`<form class="desk-form" data-desk-form="todo"><input class="input" name="task" maxlength="120" placeholder="Add a task…" aria-label="New task"><button class="btn primary" type="submit">Add</button></form><ul class="desk-tasks">${D.tasks.length ? raw(D.tasks.map((t, i) => '<li class="' + (t.done ? 'done' : '') + '"><label><input type="checkbox" data-desk="task-toggle" data-index="' + i + '" ' + (t.done ? 'checked' : '') + '><span>' + esc(t.text) + '</span></label><button type="button" data-desk="task-delete" data-index="' + i + '" aria-label="Delete task">×</button></li>').join("")) : html`<li class="desk-empty">Nothing on the list yet.</li>`}</ul>`);
    if (id === "weather") return widget(id, html`<div class="desk-form"><input class="input" data-desk-input="location" maxlength="100" value="${D.location}" placeholder="Town or city" aria-label="Weather location"><button class="btn" type="button" data-desk="weather">Get weather</button></div><div class="desk-weather">${D.weatherError ? html`<p class="desk-error">${D.weatherError}</p>` : D.weather ? html`<pre>${D.weather}</pre>` : html`<p class="desk-empty">Choose a location to load the forecast.</p>`}</div>`);
    if (id === "calendar") return widget(id, calendar());
    if (id === "notes") return widget(id, html`<textarea class="input desk-notes" data-desk-input="notes" maxlength="5000" placeholder="Jot something down…" aria-label="Desk notes">${D.notes}</textarea><div class="desk-note-status">Saved in this browser</div>`);
    if (id === "focus") return widget(id, html`<div class="desk-focus"><div class="desk-focus-time" data-focus-time>${timerText()}</div><div class="desk-muted" data-focus-mode>${D.focus.mode === "focus" ? "Focus" : "Break"}</div><div class="row"><button type="button" class="btn primary" data-desk="focus-toggle">${D.focus.running ? "Pause" : "Start"}</button><button type="button" class="btn" data-desk="focus-reset">Reset</button></div></div>`);
    if (id === "countdown") return widget(id, html`<form class="desk-countdown-form" data-desk-form="countdown"><input class="input" name="title" maxlength="60" placeholder="What are you counting down to?" value="${D.countdown ? D.countdown.title : ""}"><input class="input" name="date" type="date" value="${D.countdown ? D.countdown.date : ""}" aria-label="Countdown date"><button class="btn" type="submit">Save</button></form><div class="desk-countdown">${D.countdown && D.countdown.date ? countdownText() : "Pick a date to start a countdown."}</div>`);
    if (id === "quote") { const q = quoteToday(); return widget(id, html`<blockquote class="desk-quote">“${q[0]}”<cite>${q[1]}</cite></blockquote><span class="desk-muted">A different quote each day · picked on this device</span>`); }
    if (id === "clocks") return widget(id, html`<div class="desk-clocks">${[["Jersey", "Europe/Jersey"], ["New York", "America/New_York"], ["Tokyo", "Asia/Tokyo"], ["Sydney", "Australia/Sydney"]].map(([name, zone]) => html`<div><span>${name}</span><b data-zone="${zone}">--:--</b></div>`)}</div>`);
    if (id === "cluster") return widget(id, html`<div class="desk-cluster"><div><b data-cluster="nodes">—</b><span>ready nodes</span></div><div><b data-cluster="pods">—</b><span>running pods</span></div><div><b data-cluster="workloads">—</b><span>workloads</span></div></div><p class="desk-muted" data-cluster-note>${S.d ? "Live dashboard data" : "Waiting for cluster data"}</p>`);
    if (id === "music") return widget(id, html`<div class="desk-music-art">♫</div><p>Pick up where you left off in your library, Discover or DJ.</p><button type="button" class="btn primary" data-desk="music">Open Music</button>`);
    if (id === "links") return widget(id, html`<div class="desk-links"><button type="button" class="btn" data-desk="overview">Overview</button><button type="button" class="btn" data-desk="music">Music</button><button type="button" class="btn" data-desk="ai">AI</button><button type="button" class="btn" data-desk="galaxy">AI galaxy</button><button type="button" class="btn" data-desk="settings">Settings</button></div>`);
    if (id === "mascot") { const n = ((D.mascot % mascotLines.length) + mascotLines.length) % mascotLines.length; return widget(id, html`<button type="button" class="desk-mascot" data-desk="mascot" aria-label="Say something encouraging"><span aria-hidden="true">${["🪴", "🦊", "🐸", "🐧", "🐱"][n]}</span><b>${mascotLines[n]}</b><small>Tap me</small></button>`); }
    return "";
  }
  function timerText() { const left = D.focus.running ? Math.max(0, Math.ceil((D.focus.until - Date.now()) / 1000)) : Math.max(0, +D.focus.remaining || 0); return String(Math.floor(left / 60)).padStart(2, "0") + ":" + String(left % 60).padStart(2, "0"); }
  function countdownText() { const diff = Math.ceil((new Date(D.countdown.date + "T00:00:00") - new Date(new Date().toDateString())) / 86400000); return diff < 0 ? "That date has passed." : diff === 0 ? "Today! 🎉" : diff + " day" + (diff === 1 ? "" : "s") + " to go"; }
  function drawWidgets() { const host = $("#desk-widgets"); if (host) host.innerHTML = D.widgets.map(renderWidget).join("") || '<div class="desk-no-widgets">Add a widget to make this desk yours.</div>'; }
  function drawPicker() {
    const picker = $("#desk-picker"); if (!picker) return;
    picker.innerHTML = '<div class="desk-picker-head"><div><b>Add a widget</b><p>Choose any of the 12 desk widgets.</p></div><button class="btn icon-only" type="button" data-desk="picker-close" aria-label="Close">×</button></div><div class="desk-picker-grid">' + CATALOG.map(([id, name, sub]) => '<button type="button" class="desk-picker-item" data-desk="add" data-id="' + id + '" ' + (D.widgets.includes(id) ? 'disabled' : '') + '><b>' + esc(name) + '</b><span>' + esc(D.widgets.includes(id) ? "Already on your desk" : sub) + '</span></button>').join("") + '</div>';
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
  async function getWeather(btn) {
    const loc = D.location.trim(); if (!loc) { toast("Enter a town or city first."); $("[data-desk-input=location]")?.focus(); return; }
    btn.disabled = true; btn.textContent = "Loading…"; D.weatherError = ""; save();
    try {
      const r = await postJSON("/api/v1/web/tool", { tool: "Weather", args: { location: loc } });
      if (!r.ok) throw new Error(r.error || "Weather is unavailable.");
      D.weather = r.summary || r.text || "No forecast returned."; D.weatherError = "";
    } catch (e) { D.weatherError = e.message || "Couldn't load weather."; }
    save(); drawWidgets(); paintMode();
  }
  function click(e) {
    const b = e.target.closest("[data-desk]"); if (!b) return;
    const action = b.dataset.desk;
    if (action === "add") { const id = b.dataset.id; if (KNOWN.has(id) && !D.widgets.includes(id)) { D.widgets.push(id); save(); drawWidgets(); drawPicker(); paintMode(); } }
    else if (action === "remove") { D.widgets = D.widgets.filter((x) => x !== b.dataset.id); save(); drawWidgets(); paintMode(); }
    else if (action === "picker-close") closePicker();
    else if (action === "customize") { D.customizing = !D.customizing; save(); paintMode(); }
    else if (action === "add-widget") openPicker();
    else if (action === "task-delete") { D.tasks.splice(+b.dataset.index, 1); save(); drawWidgets(); paintMode(); }
    else if (action === "weather") getWeather(b);
    else if (action === "cal-prev" || action === "cal-next") { D.calendarOffset += action === "cal-prev" ? -1 : 1; save(); drawWidgets(); paintMode(); }
    else if (action === "focus-toggle") { if (D.focus.running) { D.focus.remaining = Math.max(0, Math.ceil((D.focus.until - Date.now()) / 1000)); D.focus.running = false; } else { D.focus.until = Date.now() + Math.max(1, D.focus.remaining) * 1000; D.focus.running = true; } save(); drawWidgets(); paintMode(); }
    else if (action === "focus-reset") { D.focus = { mode: "focus", remaining: 1500, running: false, until: 0 }; save(); drawWidgets(); paintMode(); }
    else if (action === "mascot") { D.mascot++; save(); drawWidgets(); paintMode(); }
    else if (action === "music") go("music");
    else if (["overview", "ai", "settings", "galaxy"].includes(action)) go(action);
  }
  function submit(e) {
    const form = e.target.closest("form[data-desk-form]"); if (!form) return;
    e.preventDefault(); const data = new FormData(form);
    if (form.dataset.deskForm === "todo") { const text = String(data.get("task") || "").trim(); if (!text) return; D.tasks.push({ text, done: false }); D.tasks = D.tasks.slice(-100); save(); drawWidgets(); paintMode(); $("[data-widget=todo] input[name=task]")?.focus(); }
    else if (form.dataset.deskForm === "countdown") { const date = String(data.get("date") || ""), titleText = String(data.get("title") || "").trim(); D.countdown = date ? { date, title: titleText || "Countdown" } : null; save(); drawWidgets(); paintMode(); }
  }
  function change(e) {
    const el = e.target;
    if (el.matches('[data-desk="task-toggle"]')) { const item = D.tasks[+el.dataset.index]; if (item) { item.done = el.checked; save(); el.closest("li").classList.toggle("done", el.checked); } }
  }
  function input(e) {
    const el = e.target;
    if (el.matches('[data-desk-input="notes"]')) { D.notes = el.value; save(); }
    else if (el.matches('[data-desk-input="location"]')) { D.location = el.value; save(); }
  }
  function keydown(e) { if (e.key === "Escape" && $("#desk-picker") && !$("#desk-picker").hidden) closePicker(); }
  document.addEventListener("click", click);
  document.addEventListener("submit", submit);
  document.addEventListener("change", change);
  document.addEventListener("input", input);
  document.addEventListener("keydown", keydown);

  V.desk = {
    build() {
      NY.freshHTML($("#view"), html`<main class="desk-page"><header class="desk-top"><a class="desk-brand" href="#overview">nodeyard <span>desk</span></a><div class="desk-actions"><button type="button" class="btn" data-desk="customize" aria-pressed="${D.customizing}">Arrange</button><button type="button" class="btn primary" data-desk="add-widget">＋ Add widgets</button><button type="button" class="btn" data-desk="overview">Dashboard</button></div></header><section class="desk-hero"><div class="desk-greeting" data-desk-greeting></div><time class="desk-clock" data-desk-time></time><div class="desk-date" data-desk-date></div></section><section class="desk-widgets" id="desk-widgets" aria-label="Desk widgets"></section><div class="desk-foot">Your selected dashboard wallpaper · widgets save in this browser</div><button id="desk-picker-backdrop" class="desk-picker-backdrop" type="button" data-desk="picker-close" aria-label="Close widget picker" hidden></button><section class="desk-picker" id="desk-picker" role="dialog" aria-modal="true" aria-label="Add a widget" hidden></section></main>`);
      drawWidgets(); paintMode(); tick();
    },
    update() { tick(); },
  };
  setInterval(tick, 1000);
})();

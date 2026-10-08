// The Doctor and Settings pages, and the custom background picture. Loaded after app.js.
(function () {
  "use strict";
  const { esc, html, raw, setHTML, fmt } = NY;
  const U = NY.ui;
  const { S, V, $, $$, store, chip, card, empty, toast, getJSON, postJSON, startJob } = U;
  const IMAGE_MAX = 8 * 1024 * 1024;
  // name, label, [background, card, accent, accent 2, text] for the preview
  const THEMES = [
    ["auto", "Automatic", null], ["dark", "Midnight", ["#090d1a", "#131a2e", "#7c8cff", "#22d3ee", "#e9edf7"]],
    ["light", "Daylight", ["#f3f5fb", "#ffffff", "#4f5df0", "#0891b2", "#141a30"]], ["nord", "Nord", ["#242933", "#2e3440", "#88c0d0", "#81a1c1", "#eceff4"]],
    ["dracula", "Dracula", ["#1e1f29", "#282a36", "#bd93f9", "#ff79c6", "#f8f8f2"]], ["solarized-dark", "Solarized dark", ["#00212b", "#073642", "#268bd2", "#2aa198", "#eee8d5"]],
    ["solarized-light", "Solarized light", ["#fdf6e3", "#fffcf0", "#268bd2", "#2aa198", "#073642"]], ["forest", "Forest", ["#0c1510", "#14221a", "#4ade80", "#a3e635", "#e6f2e9"]],
    ["sunset", "Sunset", ["#1a0f14", "#2a1720", "#fb923c", "#f472b6", "#fbece6"]], ["ocean", "Ocean", ["#04131c", "#0a2030", "#22d3ee", "#3b82f6", "#e3f4fb"]],
    ["rose", "Rose", ["#fff5f7", "#ffffff", "#db2777", "#9333ea", "#3b0f20"]], ["paper", "Paper", ["#f4f1ea", "#fffdf8", "#b45309", "#0f766e", "#2b2620"]],
    ["contrast", "High contrast", ["#000000", "#0a0a0a", "#ffd400", "#00e5ff", "#ffffff"]],
  ];
  function setTheme(t) {
    store.set("theme", t);
    if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  }
  const themeCard = (cur) => card("Themes", html`<p class="muted small" style="margin-top:0">Saved in this browser. Automatic follows your system's light or dark mode.</p>
    <div class="themes">${THEMES.map(([id, label, c]) => html`<button class="theme-card ${cur === id ? "on" : ""}" data-st="theme" data-theme="${id}" aria-pressed="${cur === id ? "true" : "false"}">
      <div class="sw" style="${c ? "background:" + c[0] : "background:linear-gradient(135deg,#090d1a 50%,#f3f5fb 50%)"}">${c ? raw('<i style="height:34px;background:' + c[1] + ';border:1px solid ' + c[4] + '22"></i><i style="height:24px;background:' + c[2] + '"></i><i style="height:16px;background:' + c[3] + '"></i><span style="margin-left:auto;color:' + c[4] + ';font-weight:700;font-size:15px">Aa</span>') : ""}</div>
      <div class="nm">${label}</div></button>`)}</div>`);
  const T = { settings: null, doctor: null, doctorBusy: false, newPassword: "" };

  // ------------------------------------------------------------------ background picture
  function applyBackground(bg) {
    let el = $("#ny-bg");
    if (!bg) { if (el) el.remove(); document.body.classList.remove("has-bg"); return; }
    if (!el) {
      el = document.createElement("div");
      el.id = "ny-bg";
      el.setAttribute("aria-hidden", "true");
      el.innerHTML = '<div class="img"></div><div class="dim"></div>';
      document.body.prepend(el);
    }
    el.querySelector(".img").style.backgroundImage = 'url("/api/background?v=' + encodeURIComponent(bg.v) + '")';
    el.querySelector(".img").style.filter = bg.blur ? "blur(" + bg.blur + "px)" : "";
    el.querySelector(".dim").style.opacity = String((bg.dim == null ? 45 : bg.dim) / 100);
    document.body.classList.add("has-bg");
  }
  async function loadSettings() {
    try { const r = await getJSON("/api/settings"); if (r.ok) { T.settings = r; store.set("ai.chatDefaults", JSON.stringify(r.chat_defaults || {})); NY.publicAccess = r.public || null; applyBackground(r.background); } } catch (e) { /* signed out */ }
    return T.settings;
  }
  loadSettings();

  // ------------------------------------------------------------------ doctor
  const STATUS = { ok: ["passed", "good"], pass: ["passed", "good"], issue: ["problem", "bad"], warn: ["warning", "warn"], fail: ["problem", "bad"], error: ["problem", "bad"], skip: ["skipped", ""] };
  async function loadDoctor(fresh) {
    T.doctorBusy = true; renderDoctor();
    try {
      const r = await getJSON("/api/doctor" + (fresh ? "?fresh=1" : ""));
      T.doctor = r.ok ? r.doctor : { error: r.error || "doctor didn't answer." };
    } catch (e) { T.doctor = { error: "Couldn't reach the dashboard server." }; }
    T.doctorBusy = false; renderDoctor();
  }
  function renderDoctor() {
    const el = $("#doc-body"); if (!el) return;
    const D = T.doctor;
    if (!D) return setHTML(el, html`<div class="card"><div class="empty"><div class="spin"></div>Running the checks on this server… (up to a minute)</div></div>`);
    if (D.error) return setHTML(el, html`<div class="card"><div class="empty"><b>Couldn't run doctor</b>${D.error}<div style="margin-top:12px"><button class="btn" data-st="doc-run">Try again</button></div></div></div>`);
    const checks = D.checks || [];
    const bad = checks.filter((c) => c.status !== "ok" && c.status !== "pass" && c.status !== "skip");
    const fixable = bad.filter((c) => c.fix);
    const cats = [...new Set(checks.map((c) => c.category))];
    const row = (c) => { const s = STATUS[c.status] || [c.status, "warn"]; const isBad = c.status !== "ok" && c.status !== "pass" && c.status !== "skip";
      return html`<div class="row doc-check ${isBad ? "bad" : ""}" style="align-items:flex-start;gap:12px">
        <div style="padding-top:2px">${chip(s[0], s[1])}</div>
        <div class="grow" style="min-width:0"><b>${c.title}</b>${c.detail ? html`<div class="muted small">${c.detail}</div>` : ""}${isBad && c.fix ? html`<div class="faint small">Fix: ${c.fix}</div>` : ""}</div>
        ${isBad && c.fix ? html`<button class="btn small" data-st="doc-fix" data-id="${c.id}">Fix</button>` : ""}</div>`; };
    setHTML(el, html`<div class="card"><div class="row wrap" style="gap:10px">
        ${bad.length ? chip(bad.length + " to look at", bad.some((c) => c.status === "fail" || c.status === "error") ? "bad" : "warn") : chip("all checks passed", "good")}
        <span class="muted small">${checks.length} checks on this server${T.settings && T.settings.cluster_name ? " (" + T.settings.cluster_name + ")" : ""}</span><span class="grow"></span>
        ${fixable.length > 1 ? html`<button class="btn primary" data-st="doc-fix-all">Fix all ${fixable.length}</button>` : ""}
        <button class="btn" data-st="doc-run" ${T.doctorBusy ? raw("disabled") : ""}>${T.doctorBusy ? "Checking…" : "Run again"}</button></div>
        <p class="muted small" style="margin:10px 0 0">Every fix can be undone with <span class="mono">sudo nodeyard undo</span>. Doctor checks the machine the dashboard runs on and, from there, the cluster's nodes and pods.</p></div>
      ${bad.length ? html`<div class="mt">${card("Needs a look", bad.map(row))}</div>` : ""}
      ${cats.map((cat) => html`<div class="mt">${card(cat, checks.filter((c) => c.category === cat).map(row))}</div>`)}`);
  }
  V.doctor = {
    build() { NY.freshHTML($("#view"), html`<div id="doc-body"></div>`); if (!T.doctor) loadDoctor(false); else renderDoctor(); },
    update() { },
  };

  // ------------------------------------------------------------------ settings
  const field = (label, inner, hint) => html`<label class="field-l">${label}${inner}${hint ? html`<span class="faint small" style="display:block;margin-top:4px">${hint}</span>` : ""}</label>`;
  function renderSettings() {
    const el = $("#set-body"); if (!el) return;
    const s = T.settings;
    if (!s) return setHTML(el, html`<div class="card"><div class="empty"><div class="spin"></div>Loading settings…</div></div>`);
    const theme = store.get("theme", "auto"), bg = s.background;
    const d = s.chat_defaults || {};
    const gateNets = s.gate ? s.gate.trusted.join("\n") : "127.0.0.0/8\n10.0.0.0/8\n172.16.0.0/12\n192.168.0.0/16\n100.64.0.0/10\n::1/128\nfc00::/7\nfd7a:115c:a1e0::/48";
    NY.freshHTML(el, html`${s.demo ? html`<div class="banner">Demo: changes here aren't saved anywhere.</div>` : ""}
      ${themeCard(theme)}
      ${card("AI chat defaults", html`
        <p class="muted small" style="margin-top:0">Saved on this server. New chats inherit these choices; each chat keeps its own settings after that.</p>
        ${field("System prompt", html`<textarea class="input" id="st-chat-system" rows="3" maxlength="12000" placeholder="e.g. You are a concise assistant.">${d.system || ""}</textarea>`)}
        <div class="row wrap" style="gap:18px;align-items:flex-end">
          ${field(html`Creativity <b id="st-chat-temp-v">${d.temperature == null ? 0.7 : d.temperature}</b>`, html`<input type="range" id="st-chat-temp" min="0" max="2" step="0.1" value="${d.temperature == null ? 0.7 : d.temperature}" class="range">`)}
          ${field("Default reply length (tokens)", html`<span class="row" style="gap:10px"><input class="input" id="st-chat-max" type="number" min="16" max="65536" step="16" value="${d.max_tokens || 1024}" style="width:130px" ${d.max_tokens === 0 ? raw("disabled") : ""}>
            <span class="check"><input type="checkbox" id="st-chat-nolimit" ${d.max_tokens === 0 ? raw("checked") : ""}> <span>No limit</span></span></span>`)}</div>
        <div class="row wrap" style="gap:18px;align-items:flex-end">
          ${field("Default model context length", html`<input class="input" id="st-chat-ctx" type="number" min="512" max="131072" step="512" value="${d.context_length || 8192}" style="width:150px">`, "Used when you start a downloaded model. A running model keeps its current context until it is started again.")}
          ${field("Context compression", html`<select class="select" id="st-chat-compress"><option value="auto" ${d.compress !== "off" ? raw("selected") : ""}>Automatic</option><option value="off" ${d.compress === "off" ? raw("selected") : ""}>Off</option></select>`)}
          ${field("Files", html`<select class="select" id="st-chat-files"><option value="ask" ${!d.files || d.files === "ask" ? raw("selected") : ""}>Only when I ask</option><option value="always" ${d.files === "always" ? raw("selected") : ""}>Always create files</option><option value="never" ${d.files === "never" ? raw("selected") : ""}>Never</option></select>`)}</div>
        <div class="row wrap" style="gap:14px;margin:8px 0">
          <label class="check"><input type="checkbox" id="st-chat-web" ${d.web ? raw("checked") : ""}> <span>Search the web first</span></label>
          <label class="check"><input type="checkbox" id="st-chat-skills" ${d.skills_auto !== false ? raw("checked") : ""}> <span>Choose AI skills automatically</span></label>
          <label class="check"><input type="checkbox" id="st-chat-autofix" ${d.autofix ? raw("checked") : ""}> <span>Run and fix code automatically</span></label>
        </div>
        <button class="btn primary" data-st="chat-defaults">Save defaults</button>`)}
      <div class="grid g-2 mt">
      ${card("Sign-in", s.auth ? html`
        ${field("New password", html`<input class="input" type="password" id="st-pw1" autocomplete="new-password">`)}
        ${field("Type it again", html`<input class="input" type="password" id="st-pw2" autocomplete="new-password">`, (s.weak_password ? "Any length." : "6 characters at least.") + " Other browsers have to sign in again; this one stays signed in.")}
        <div class="row wrap" style="gap:8px"><button class="btn primary" data-st="pw-set">Change password</button><button class="btn" data-st="pw-random">Make a random one</button></div>
        ${T.newPassword ? html`<div class="cmd mt"><span class="mono">${T.newPassword}</span><button class="btn small" data-copy="${T.newPassword}">Copy</button></div><p class="small" style="color:var(--warn)">Write it down: it is only shown now (or later on the server: sudo nodeyard dashboard password --show).</p>` : ""}
        <label class="check mt"><input type="checkbox" data-st="weak" ${s.weak_password ? raw("checked") : ""}> <span>Allow any password, with no minimum length or strength</span></label>
        ${s.weak_password ? html`<p class="small" style="color:var(--warn);margin:6px 0 0">${s.public && s.public.dashboard ? html`<b>The dashboard is public</b>, so anyone on the internet can try to guess a short password (they get 5 tries per 5 minutes, and 30 for everyone together).` : "If you make the dashboard public, a short password can be guessed from the internet."}</p>` : ""}
        <div class="row wrap mt" style="gap:8px;align-items:center"><button class="btn danger" data-st="signout-all">Sign out everywhere</button><span class="muted small">${s.sessions} signed-in browser${s.sessions === 1 ? "" : "s"}</span></div>`
        : empty("No sign-in", "This dashboard only answers on its own machine, so it has no password."))}
      ${card("Background", html`
        <div class="field-l">Background picture
          <div class="row wrap" style="gap:8px;margin-top:6px"><label class="btn" for="st-bg-file">${bg ? "Change picture…" : "Upload a picture…"}</label><input type="file" id="st-bg-file" accept="image/png,image/jpeg,image/webp" hidden>
          ${bg ? html`<button class="btn danger" data-st="bg-remove">Remove</button>` : ""}</div>
          <span class="faint small" style="display:block;margin-top:4px">PNG, JPEG or WebP, up to 8 MB. Stored on the server and only shown to signed-in browsers.</span></div>
        ${bg ? html`${field(html`Blur <span class="faint" id="st-blur-v">${bg.blur}px</span>`, html`<input type="range" id="st-blur" min="0" max="40" value="${bg.blur}" class="range">`)}
          ${field(html`Darken <span class="faint" id="st-dim-v">${bg.dim}%</span>`, html`<input type="range" id="st-dim" min="0" max="95" value="${bg.dim}" class="range">`, "Darker keeps text easy to read.")}` : ""}`)}
      ${card("One shared API key", html`
        <p class="muted small" style="margin-top:0">Use one key for Nodeyard AI, yardcode, the dashboard control API and every split model. The dashboard keeps the running model and this server key in sync. Ollama has no native API-key setting; requests to it go through this key-protected dashboard API. The dashboard sign-in password is separate.${s.gate ? " From your own networks (see Model gate) no API key is needed." : ""}</p>
        <div class="row wrap" style="gap:8px"><button class="btn" data-st="key-reveal">Reveal shared key</button><button class="btn" data-st="key-rotate">Generate a new shared key</button></div>
        <div id="st-key-shown"></div>
        ${field("Or set your own", html`<input class="input mono" id="st-key" placeholder="at least 16 characters" spellcheck="false" autocomplete="off">`)}
        <button class="btn" data-st="key-set">Set the shared key</button>
        ${s.split ? html`<div class="row wrap" style="gap:8px;margin-top:12px"><button class="btn" data-st="key-adopt">Use the running model's key everywhere</button></div>
          <p class="faint small">If your current key works with the model, this makes it the shared key for the dashboard API and yardcode too. The model stays on that key.</p>` : ""}
        <p class="faint small">Changing it restarts the model's front end (about a minute). Update Nodeyard AI and yardcode to use the new shared key.</p>`)}
      ${card("Kubernetes", html`
        <p class="muted small" style="margin-top:0">Restart Kubernetes (k3s) on every machine: workers one at a time, then the control node. Running containers keep running; the cluster is out of reach for a minute or two. Use it when nodes act stuck.</p>
        <button class="btn danger" data-st="k8s-restart">Restart Kubernetes…</button>
        <p class="muted small">For a complete machine restart, workers are drained and rebooted one at a time, then the control server. This interrupts workloads and takes the whole cluster offline briefly.</p>
        <button class="btn danger" data-st="k8s-reboot">Reboot every machine…</button>
        <p class="faint small">Takes a few minutes. Only works from your own network or Tailscale.</p>`)}
      ${card("Hugging Face", html`
        <p class="muted small" style="margin-top:0">Only needed for models you have to accept terms for. ${s.hf_token ? chip("token set", "good") : chip("no token")}</p>
        ${field("Access token", html`<input class="input mono" id="st-hf" placeholder="hf_…" spellcheck="false" autocomplete="off">`, raw('Make one at <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener noreferrer">huggingface.co/settings/tokens</a> (read access is enough).'))}
        <div class="row wrap" style="gap:8px"><button class="btn primary" data-st="hf-set">Save token</button>${s.hf_token ? html`<button class="btn danger" data-st="hf-remove">Remove</button>` : ""}</div>`)}
      ${card("Dashboard service", html`
        ${field("Listen on", html`<input class="input mono" id="st-listen" value="${s.listen}" spellcheck="false">`, "auto (this machine + Tailscale), local, tailscale, all, or IP addresses with commas.")}
        <div class="row wrap" style="gap:12px"><div class="grow">${field("Port", html`<input class="input" type="number" id="st-port" min="1024" max="65535" value="${s.port}">`)}</div>
          <div class="grow">${field("Refresh every (seconds)", html`<input class="input" type="number" id="st-int" min="1" max="300" value="${Math.round(s.interval)}">`)}</div></div>
        <button class="btn primary" data-st="svc-apply">Apply and restart</button>
        <p class="faint small">The dashboard restarts (a few seconds) and the page reconnects by itself. Careful: “local” means only the server itself can open it.</p>`)}
      ${card("Model gate", html`
        <p class="muted small" style="margin-top:0">${s.gate ? html`${chip("on", "good")} Requests from these networks need no API key; everywhere else still does.` : html`${chip("off")} The server API key is needed from everywhere.`}</p>
        ${field("No key needed from (one network per line)", html`<textarea class="input mono" id="st-gate" rows="6" spellcheck="false">${gateNets}</textarea>`)}
        <div class="row wrap" style="gap:8px"><button class="btn primary" data-st="gate-apply" ${s.split ? "" : raw("disabled")}>${s.gate ? "Update the gate" : "Turn the gate on"}</button>${s.gate ? html`<button class="btn danger" data-st="gate-remove">Turn it off</button>` : ""}</div>
        ${s.split ? "" : html`<p class="faint small">Run a split model first.</p>`}`)}
      ${card("Public access", publicCard(s.public))}
      ${card("Node agents", html`
        <p class="muted small" style="margin-top:0">${s.agents ? chip("installed", "good") : chip("not installed")} A small read-only helper on every node: processes, CPU clocks, temperatures and memory detail.</p>
        <div class="row wrap" style="gap:8px">${s.agents ? html`<button class="btn" data-st="agent-install">Reinstall</button><button class="btn danger" data-st="agent-remove">Remove</button>` : html`<button class="btn primary" data-st="agent-install">Install</button>`}</div>`)}
      ${card("Disk limits", html`
        <p class="muted small" style="margin-top:0">How full nodeyard may let a node's disk get with models, weight caches and Ollama. Empty = no limit.</p>
        ${s.nodes.map((n) => html`<div class="row" style="gap:8px;margin:6px 0"><span class="grow">${n.name}</span><input class="input" style="width:90px" type="number" min="1" placeholder="none" data-limit="${n.name}" value="${n.disk_limit || ""}"><span class="muted small">GiB</span><button class="btn small" data-st="limit-set" data-node="${n.name}">Set</button></div>`)}`)}
      ${card("Cluster name", html`
        ${field("Name", html`<input class="input" id="st-name" value="${s.cluster_name}" placeholder="homelab" spellcheck="false">`, "Lowercase letters, digits and dashes. Shown in the sidebar after the dashboard restarts.")}
        <button class="btn" data-st="name-set">Save</button>`)}
      </div>`);
  }
  function publicCard(p) {
    if (!p || !p.available) return html`<p class="muted small" style="margin-top:0">Reach the dashboard and model API from anywhere on the internet, without Tailscale on that device (Tailscale Funnel).</p>
      <p class="small" style="color:var(--warn)">Tailscale isn't connected on this server, so this isn't available.</p>`;
    const row = (label, url, what, need) => html`<div class="row wrap" style="gap:8px;margin:8px 0;align-items:center"><b style="min-width:92px">${label}</b>
      ${url ? html`${chip("public", "warn")}<span class="chip btnlike mono" data-copy="${url}">${url}</span><span class="grow"></span><button class="btn small danger" data-st="public-off" data-what="${what}">Turn off</button>`
        : html`${chip("private")}<span class="grow"></span><button class="btn small" data-st="public-on" data-what="${what}">Make public</button>`}</div>
      ${url ? html`<div class="faint small" style="margin:-4px 0 8px 100px">${need}</div>` : ""}`;
    return html`<p class="muted small" style="margin-top:0">Reach these from anywhere on the internet (a phone on mobile data, a work laptop), with no Tailscale needed there. Through Tailscale Funnel, with a real HTTPS certificate.</p>
      ${row("Dashboard", p.dashboard, "dashboard", "Asks for the dashboard password. Wrong passwords from the internet can't lock you out at home.")}
      ${row("Model API", p.api, "api", "Every request from the internet needs the server API key, even if your own network doesn't.")}
      <p class="faint small">Anyone can find a public address, so use a strong dashboard password (Sign-in → Make a random one). Turning the dashboard on needs one.</p>`;
  }
  V.settings = {
    build() { NY.freshHTML($("#view"), html`<div id="set-body"></div>`); renderSettings(); loadSettings().then(renderSettings); },
    update() { },
  };

  async function post(url, body) {
    try { const r = await postJSON(url, body || {}); if (!r.ok) toast(r.error || "That didn't work."); return r; } catch (e) { return { ok: false }; }
  }
  const readFile = (file) => new Promise((resolve, reject) => { const fr = new FileReader(); fr.onload = () => resolve(fr.result); fr.onerror = reject; fr.readAsDataURL(file); });

  document.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-st]"); if (!el) return;
    const a = el.dataset.st, s = T.settings;
    if (a === "doc-run") loadDoctor(true);
    else if (a === "doc-fix") startJob("doctor-fix", { only: el.dataset.id }, () => loadDoctor(true));
    else if (a === "doc-fix-all") startJob("doctor-fix", {}, () => loadDoctor(true));
    else if (a === "theme") { setTheme(el.dataset.theme); renderSettings(); }
    else if (a === "chat-defaults") {
      const noLimit = $("#st-chat-nolimit").checked;
      const defaults = {
        system: $("#st-chat-system").value, temperature: +$("#st-chat-temp").value,
        max_tokens: noLimit ? 0 : +$("#st-chat-max").value,
        compress: $("#st-chat-compress").value, files: $("#st-chat-files").value,
        context_length: +$("#st-chat-ctx").value,
        web: $("#st-chat-web").checked, skills_auto: $("#st-chat-skills").checked,
        autofix: $("#st-chat-autofix").checked,
      };
      const r = await post("/api/settings/chat-defaults", { defaults });
      if (r.ok) { T.settings.chat_defaults = r.chat_defaults; store.set("ai.chatDefaults", JSON.stringify(r.chat_defaults)); toast("AI chat defaults saved for new chats."); renderSettings(); }
    }
    else if (a === "pw-set") {
      const r = await post("/api/settings/password", { password: $("#st-pw1").value, again: $("#st-pw2").value });
      if (r.ok) { T.newPassword = ""; toast("Password changed. Other browsers have to sign in again."); loadSettings().then(renderSettings); }
    } else if (a === "pw-random") {
      const r = await post("/api/settings/password", { random: true });
      if (r.ok) { T.newPassword = r.password; renderSettings(); }
    } else if (a === "weak") {
      const on = el.checked;
      if (on && !window.confirm("Allow any dashboard password, however short?\n\nOnce the dashboard is public, a weak password can be guessed from the internet.")) { el.checked = false; return; }
      const r = await post("/api/settings/weak-password", { on });
      if (r.ok) loadSettings().then(renderSettings); else el.checked = !on;
    } else if (a === "signout-all") {
      const r = await post("/api/settings/signout-all");
      if (r.ok) location.href = "/login";
    } else if (a === "key-reveal") {
      const r = await post("/api/ai/reveal-key");
      if (r.ok) setHTML($("#st-key-shown"), html`<div class="cmd mt"><span class="mono">${r.key}</span><button class="btn small" data-copy="${r.key}">Copy</button></div>`);
    } else if (a === "key-rotate") startJob("key-rotate", {}, () => setHTML($("#st-key-shown"), ""));
    else if (a === "key-set") { const r = await post("/api/settings/model-key", { key: $("#st-key").value }); if (r.ok) { $("#st-key").value = ""; toast("New key saved; the model restarts with it."); } }
    else if (a === "key-adopt") {
      if (!window.confirm("Use the running model's current API key as the server-wide key?\n\nThe model won't restart or change keys. Yardcode and the dashboard control API will use this key too.")) return;
      const r = await post("/api/settings/adopt-model-key", {});
      if (r.ok) { toast("The server key now matches the running model. Keep using your current key in yardcode."); loadSettings().then(renderSettings); }
    }
    else if (a === "k8s-restart") {
      if (!window.confirm("Restart Kubernetes on every node?\n\nWorkers restart one at a time, then the control node. Containers keep running, but the cluster is unreachable for a minute or two.")) return;
      startJob("restart-cluster", {}, () => U.load(true));
    } else if (a === "k8s-reboot") {
      if (!window.confirm("Fully reboot every machine in the Kubernetes cluster?\n\nWorkers are drained and rebooted one at a time; the control server reboots last. Every workload will be interrupted, and the dashboard will go offline briefly.")) return;
      startJob("reboot-cluster", {}, () => U.load(true));
    } else if (a === "hf-set") { const r = await post("/api/settings/hf-token", { token: $("#st-hf").value }); if (r.ok) { toast("Token saved."); loadSettings().then(renderSettings); } }
    else if (a === "hf-remove") { const r = await post("/api/settings/hf-token", { remove: true }); if (r.ok) loadSettings().then(renderSettings); }
    else if (a === "svc-apply") {
      const port = +$("#st-port").value, body = { listen: $("#st-listen").value, port, interval: +$("#st-int").value };
      const r = await post("/api/settings/service", body);
      if (r.ok && r.restarting) {
        toast("Restarting the dashboard…");
        if (port !== +location.port) setTimeout(() => { location.href = location.protocol + "//" + location.hostname + ":" + port + "/#settings"; }, 6000);
      } else if (r.ok) toast("Saved (demo).");
    } else if (a === "gate-apply") {
      const nets = $("#st-gate").value.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      startJob("gate-install", { trusted: nets }, () => { U.load(true); loadSettings().then(renderSettings); });
    } else if (a === "gate-remove") startJob("gate-remove", {}, () => { U.load(true); loadSettings().then(renderSettings); });
    else if (a === "agent-install" || a === "agent-remove") startJob(a, {}, () => loadSettings().then(renderSettings));
    else if (a === "limit-set") { const v = ($('[data-limit="' + el.dataset.node + '"]') || {}).value; startJob("disk-limit", { node: el.dataset.node, gib: v ? String(v) : "off" }, () => { U.load(true); loadSettings().then(renderSettings); }); }
    else if (a === "public-on") {
      const what = el.dataset.what, label = what === "api" ? "the model API" : "the dashboard";
      const ok = window.confirm("Make " + label + " reachable from anywhere on the internet?\n\n" + (what === "api" ? "Every request from the internet must carry the server API key." : "Anyone who finds the address sees the sign-in page; the password protects it.") + "\n\nYou can turn it off again here.");
      if (ok) startJob("public-on", { what }, async () => { await post("/api/settings/public-refresh"); loadSettings().then(renderSettings); });
    } else if (a === "public-off") startJob("public-off", { what: el.dataset.what }, async () => { await post("/api/settings/public-refresh"); loadSettings().then(renderSettings); });
    else if (a === "name-set") startJob("cluster-name", { name: $("#st-name").value.trim() });
    else if (a === "bg-remove") { const r = await post("/api/settings/background-remove"); if (r.ok) { applyBackground(null); loadSettings().then(renderSettings); } }
    void s;
  });
  document.addEventListener("change", async (e) => {
    if (e.target.id === "st-chat-nolimit") { $("#st-chat-max").disabled = e.target.checked; return; }
    if (e.target.id !== "st-bg-file") return;
    const f = e.target.files && e.target.files[0]; if (!f) return;
    if (f.size > IMAGE_MAX) { toast("That picture is over 8 MB."); return; }
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { toast("Use a PNG, JPEG or WebP picture."); return; }
    toast("Uploading…");
    let data; try { data = await readFile(f); } catch (err) { toast("Couldn't read that file."); return; }
    const r = await post("/api/settings/background", { image: data });
    if (r.ok) { applyBackground(r.background); loadSettings().then(renderSettings); toast("Background set."); }
  });
  let styleTimer = null;
  document.addEventListener("input", (e) => {
    if (e.target.id === "st-chat-temp") { $("#st-chat-temp-v").textContent = e.target.value; return; }
    if (e.target.id !== "st-blur" && e.target.id !== "st-dim") return;
    const s = T.settings; if (!s || !s.background) return;
    const blur = +$("#st-blur").value, dim = +$("#st-dim").value;
    $("#st-blur-v").textContent = blur + "px"; $("#st-dim-v").textContent = dim + "%";
    applyBackground(Object.assign({}, s.background, { blur, dim }));
    clearTimeout(styleTimer);
    styleTimer = setTimeout(async () => { const r = await post("/api/settings/background-style", { blur, dim }); if (r.ok) s.background = r.background; }, 400);
  });
  void esc; void fmt;
})();

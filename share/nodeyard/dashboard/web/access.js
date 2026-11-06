// Network access, the one-host device setup flow, and a short no-change tour.
(function () {
  "use strict";
  const { esc, html, raw, setHTML } = NY;
  const U = NY.ui;
  const { S, V, $, card, chip, toast, getJSON, postJSON, startJob, go } = U;
  const N = { settings: null, error: "", loaded: false };
  const D = { host: "", user: "", port: "22", iface: "", probe: null, fingerprint: "", verified: false, error: "", busy: false, job: "" };
  const T = { step: 0 };

  function bindHas(s, address) {
    const list = (s.listen_addrs || []).map(String);
    return list.includes(address) || list.includes("0.0.0.0") || list.includes("::") || s.listen === "all";
  }
  function renderAccess() {
    const root = $("#view");
    if (!root || S.view !== "access") return;
    if (!N.settings) {
      setHTML(root, card("Network access", N.error ? html`<div class="note bad">${N.error}</div>` : html`<div class="empty"><div class="spin"></div>Reading network settings…</div>`));
      return;
    }
    const s = N.settings, port = Number(s.port) || 9092, addresses = s.network_addresses || [];
    const lan = addresses.filter((a) => a.kind === "lan"), tail = addresses.filter((a) => a.kind === "tailnet");
    const current = String(s.listen || "");
    const hasLan = lan.some((a) => current.includes(a.address));
    const hasTail = tail.some((a) => current.includes(a.address)) || current === "tailscale" || current === "auto";
    const scope = current === "local" ? "local" : hasLan && hasTail ? "lan-tailnet" : hasTail ? "tailnet" : "custom";
    const serve = s.https && s.https.url ? String(s.https.url).replace(/\/$/, "") : "";
    const publicApi = s.public && s.public.api;
    const urlRows = [];
    if (serve) urlRows.push({ label: "Tailscale HTTPS", url: serve + "/api/v1", state: "private tailnet" });
    tail.forEach((a) => urlRows.push({ label: "Tailscale address", url: "http://" + a.address + ":" + port + "/api/v1", state: bindHas(s, a.address) ? "listening" : "not enabled" }));
    lan.forEach((a) => urlRows.push({ label: "LAN address", url: "http://" + a.address + ":" + port + "/api/v1", state: bindHas(s, a.address) ? "listening" : "not enabled" }));
    const base = location.origin + "/api/v1";
    setHTML(root, html`<div class="grid g-2">
      ${card("Dashboard and API reach", html`<p class="muted small" style="margin-top:0">Choose where this dashboard listens. The same control API stays at <code>/api/v1</code> on each enabled address and uses the same shared API key.</p>
        <div class="row wrap" style="gap:8px;margin:12px 0">
          <button class="btn ${scope === "local" ? "primary" : ""}" data-net-scope="local">This device only</button>
          <button class="btn ${scope === "tailnet" ? "primary" : ""}" data-net-scope="tailnet">This device + Tailscale</button>
          <button class="btn ${scope === "lan-tailnet" ? "primary" : ""}" data-net-scope="lan-tailnet" ${!lan.length || !tail.length ? raw("disabled") : ""}>LAN + Tailscale</button>
        </div>
        <div class="small"><span class="muted">Current listener:</span> <code>${esc(s.listen || "unknown")}</code> ${s.listen_addrs && s.listen_addrs.length ? html`<span class="faint">(${s.listen_addrs.map(esc).join(", ")})</span>` : ""}</div>
        <p class="faint small">Remote listeners require dashboard sign-in. LAN HTTP is for a trusted local network; Tailscale traffic is carried inside your encrypted tailnet. Applying a scope restarts the dashboard.</p>
        <button class="btn primary" data-net-apply>Apply selected scope</button>`, html`<button class="btn small" data-open="tour">Take the tour</button>`)}
      ${card("API addresses", html`<p class="muted small" style="margin-top:0">Use one base URL from the same server. API clients send <code>Authorization: Bearer &lt;shared-key&gt;</code>.</p>
        <div class="mono small" style="margin-bottom:8px"><span class="faint">This browser</span><br><span data-copy="${base}">${base}</span></div>
        ${urlRows.length ? urlRows.map((r) => html`<div class="row wrap" style="gap:8px;margin:8px 0;align-items:center"><b class="small" style="min-width:120px">${r.label}</b><span class="mono small" data-copy="${r.url}">${r.url}</span><span class="grow"></span>${chip(r.state, r.state === "listening" || r.state === "private tailnet" ? "good" : "warn")}</div>`) : html`<div class="empty">No LAN or Tailscale interface address was found.</div>`}
        <p class="faint small">The key is not displayed here. Find or rotate it under Settings → One shared API key. Do not paste it into a URL.</p>`)}
    </div>
    <div class="grid g-2 mt">
      ${card("Tailscale tunnels", html`<p class="muted small" style="margin-top:0">Tailscale Serve gives this dashboard HTTPS access to tailnet devices only. It does not open a router port.</p>
        ${serve ? html`<div class="row wrap" style="gap:8px">${chip("Serve enabled", "good")}<a class="mono" href="${serve}">${serve}</a></div>` : s.https && s.https.tailscale === false ? html`<div class="note warn">Tailscale is not installed on this server.</div>` : html`<button class="btn primary" data-net-action="https-on">Enable Tailscale Serve</button><p class="faint small">If Serve cannot start, enable MagicDNS and HTTPS certificates in your Tailscale admin console.</p>`}
        <div class="rule"></div><p class="small"><b>SSH tunnel</b> (temporary, private):</p><div class="cmd">ssh -L ${port}:127.0.0.1:${port} user@NODEYARD_HOST<button class="btn small" data-copy="ssh -L ${port}:127.0.0.1:${port} user@NODEYARD_HOST">Copy</button></div>
        <p class="faint small">Replace <code>user@NODEYARD_HOST</code> with an SSH account and this server's LAN or Tailscale address.</p>`)}
      ${card("Public internet access", html`<p class="muted small" style="margin-top:0">Tailscale Funnel is separate from private Serve. It makes an endpoint reachable from the public internet; the model API still requires the shared API key.</p>
        ${publicApi ? html`<div class="row wrap" style="gap:8px">${chip("Funnel API public", "warn")}<span class="mono small" data-copy="${publicApi}">${publicApi}</span></div><p class="faint small">Manage public endpoints in Settings. No router port forwarding is used.</p>` : s.public && s.public.available ? html`<button class="btn" data-net-action="public-api">Enable public model API (Funnel)</button><p class="faint small">This requires a separate confirmation. It exposes the model API to the internet, not the dashboard control API.</p>` : html`<div class="note">Funnel is unavailable until Tailscale is connected.</div>`}
        <p class="faint small">Avoid router port forwards. Use Serve for tailnet-only access; use Funnel only when you intentionally want a public endpoint.</p>`)}
    </div>
    <div id="nic-health"></div>`);
    renderLinkHealth();
  }
  function renderLinkHealth() {
    const root = $("#nic-health");
    if (!root || S.view !== "access") return;
    const nodes = S.d && S.d.nodes || [];
    const rows = nodes.flatMap((node) => ((node.hw || {}).hardware || {}).nics || [])
      .filter((nic) => !nic.wireless && !/^tailscale|^wg/.test(nic.name || ""))
      .map((nic) => {
        const node = nodes.find((item) => (((item.hw || {}).hardware || {}).nics || []).includes(nic));
        const status = nic.carrier === false ? ["link down", ""] : nic.duplex === "full" ? ["full duplex", "good"]
          : nic.duplex === "half" ? ["half duplex", "warn"] : ["unknown", ""];
        return html`<div class="row wrap" style="gap:8px;padding:7px 0;border-bottom:1px solid var(--line)"><b>${esc(node && node.name || "Device")}</b><code>${esc(nic.name || "")}</code>${nic.usb ? chip("USB", "accent") : chip("on-board")}<span class="faint small">${nic.speed_mbps ? nic.speed_mbps + " Mb/s" : "speed unknown"}${nic.max_mbps ? " · max " + nic.max_mbps + " Mb/s" : ""}</span><span class="grow"></span>${chip(status[0], status[1])}</div>`;
      });
    setHTML(root, card("Ethernet link health", html`<p class="muted small" style="margin-top:0">Read-only link status from each device. Several clients sending at once do not lower duplex; half duplex or a mismatch is between the NIC and its switch port. This page never forces link settings.</p>
      ${rows.length ? rows : html`<div class="empty">No wired NIC link readings are available yet. Install the read-only node agent to see duplex and USB adapter status.</div>`}
      <p class="faint small">For a mismatch, check that both the switch port and the NIC use compatible auto-negotiation settings. Do not force speed or duplex from one side only.</p>`));
  }
  async function loadAccess() {
    N.error = ""; N.loaded = false; N.settings = null; renderAccess();
    try {
      const r = await getJSON("/api/settings");
      if (!r || !r.ok) throw new Error(r && r.error || "Couldn't read network settings.");
      N.settings = r; N.loaded = true;
    } catch (e) { N.error = e.message || "Couldn't reach the dashboard."; }
    renderAccess();
  }
  V.access = { build() { loadAccess(); }, update() { renderLinkHealth(); } };

  function renderSetup() {
    const root = $("#view");
    if (!root || S.view !== "setup") return;
    const fingerprints = D.probe && D.probe.fingerprints || [];
    const canAdd = !!(D.probe && D.fingerprint && D.probe.fingerprints.some((item) => item.fingerprint === D.fingerprint) && D.verified && D.user && /^[A-Za-z_][A-Za-z0-9._-]{0,31}$/.test(D.user)
      && (!D.iface || /^[A-Za-z0-9_.-]{1,15}$/.test(D.iface)) && !D.busy);
    setHTML(root, html`<div class="grid g-2">
      ${card("One host, more devices", html`<p class="muted small" style="margin-top:0">Keep Nodeyard on this machine and use its dashboard to install and join worker devices over SSH. You do not need to open the router or copy the cluster token by hand.</p>
        <ol class="small"><li>Enter the new device's private LAN or Tailscale IPv4 address.</li><li>Probe its SSH host key, then compare the fingerprint with that device through a trusted channel.</li><li>Confirm the worker install. Nodeyard copies its installer and joins it to this cluster.</li></ol>
        <div class="note warn small">The web flow uses SSH keys already available to the Nodeyard host. It never asks for a password. The target must allow non-interactive administrator access; otherwise use the Terminal or run <code>nodeyard add-node worker</code> from a shell.</div>
        <div class="row mt" style="gap:8px"><button class="btn" data-open="tour">Take the guided tour</button><button class="btn" data-open="access">Network access</button></div>`)}
      ${card("Add a worker", html`<div class="grid g-3">
        <label class="field-l">Private LAN or Tailscale IPv4<input id="setup-host" class="input" value="${D.host}" placeholder="192.168.1.25 or 100.x.y.z" spellcheck="false"></label>
        <label class="field-l">SSH user<input id="setup-user" class="input" value="${D.user}" placeholder="admin user" spellcheck="false" autocomplete="off"></label>
        <label class="field-l">SSH port<input id="setup-port" class="input" type="number" min="1" max="65535" value="${D.port}"></label>
      </div>
      <label class="field-l mt">Cluster network interface (optional)<input id="setup-iface" class="input" value="${D.iface}" placeholder="leave blank to use the route to this server" spellcheck="false"><span class="faint small">This selects the target device's interface for the cluster. It does not change Ethernet duplex or bonding.</span></label>
      <div class="row wrap mt" style="gap:8px"><button class="btn primary" data-setup="probe" ${D.busy ? raw("disabled") : ""}>${D.busy ? "Checking…" : "Probe SSH fingerprint"}</button>${D.probe ? chip("Key checked", "good") : ""}</div>
      ${D.error ? html`<div class="note bad mt">${D.error}</div>` : ""}
      ${fingerprints.length ? html`<div class="mt"><b>Confirm the host key</b><p class="faint small">Compare one fingerprint below with the fingerprint shown on the target device's console. The probe only reads SSH public keys; it does not log in or change the device.</p>
        ${fingerprints.map((f) => html`<label class="check"><input type="radio" name="setup-fingerprint" value="${f.fingerprint}" ${D.fingerprint === f.fingerprint ? raw("checked") : ""} data-setup-fp><span><b>${f.algorithm}</b> <code>${f.fingerprint}</code></span></label>`)}
        <label class="check mt"><input type="checkbox" id="setup-verified" ${D.verified ? raw("checked") : ""} data-setup-verified><span>I compared this fingerprint with the device through a trusted channel.</span></label>
      </div>` : ""}
      <div class="row mt" style="gap:8px"><button class="btn primary" data-setup="add" ${canAdd ? "" : raw("disabled")}>Install Nodeyard and add worker</button>${D.job ? html`<span class="faint small">Last job: ${D.job}</span>` : ""}</div>`)}
    </div>`);
  }
  function collectSetup() {
    D.host = $("#setup-host") ? $("#setup-host").value.trim() : D.host;
    D.user = $("#setup-user") ? $("#setup-user").value.trim() : D.user;
    D.port = $("#setup-port") ? $("#setup-port").value.trim() : D.port;
    D.iface = $("#setup-iface") ? $("#setup-iface").value.trim() : D.iface;
    const fp = $("input[name='setup-fingerprint']:checked");
    if (fp) D.fingerprint = fp.value;
    const verified = $("#setup-verified");
    if (verified) D.verified = verified.checked;
  }
  V.setup = { build() { renderSetup(); }, update() {} };

  const TOUR = [
    { title: "Overview", body: "Start here to see cluster health, recent alerts, and what needs attention. A NotReady node is different from a model that is simply unloaded.", page: "overview", label: "Open overview" },
    { title: "Devices and connections", body: "Nodes shows CPU, memory, and addresses. Connections checks LAN and Tailscale paths separately. Add a worker from this host when you want more compute.", page: "setup", label: "Open device setup" },
    { title: "AI models and the API", body: "Ollama replicas can spread requests across nodes; a split model pools memory across nodes. The OpenAI-compatible gateway lists installed models and can load an unloaded model when requested.", page: "ai", label: "Open AI" },
    { title: "Network access", body: "Choose local, tailnet, or trusted LAN plus tailnet access for the same /api/v1 endpoint. Serve stays private to your tailnet. Funnel is a separate public endpoint and needs explicit confirmation.", page: "access", label: "Open network access" },
    { title: "Safe operations", body: "Nodeyard keeps network changes opt-in. The setup wizard checks an SSH fingerprint before adding a worker. Public access and disruptive actions ask before they run.", page: "settings", label: "Open settings" },
  ];
  function renderTour() {
    const root = $("#view");
    if (!root || S.view !== "tour") return;
    const step = TOUR[Math.max(0, Math.min(TOUR.length - 1, T.step))];
    setHTML(root, card("Guided tour · " + (T.step + 1) + " of " + TOUR.length, html`<div class="muted" style="max-width:760px"><h2 style="margin-top:4px">${step.title}</h2><p>${step.body}</p>
      <div class="row wrap mt" style="gap:8px"><button class="btn" data-tour="back" ${T.step === 0 ? raw("disabled") : ""}>Back</button>${T.step < TOUR.length - 1 ? html`<button class="btn primary" data-tour="next">Next</button>` : html`<button class="btn primary" data-tour="finish">Finish</button>`}<button class="btn" data-open="${step.page}">${step.label}</button></div>
      <div class="row mt" style="gap:5px">${TOUR.map((_, i) => html`<span class="dot ${i === T.step ? "on" : ""}" style="width:8px;height:8px;border-radius:50%;background:${i === T.step ? "var(--accent)" : "var(--line)"}"></span>`)}</div>
    </div>`));
  }
  V.tour = { build() { T.step = 0; renderTour(); }, update() {} };

  document.addEventListener("click", async (event) => {
    const nav = event.target.closest("[data-open]");
    if (nav) { go(nav.dataset.open); return; }
    const scope = event.target.closest("[data-net-scope]");
    if (scope) { scope.parentElement.querySelectorAll("[data-net-scope]").forEach((b) => b.classList.remove("primary")); scope.classList.add("primary"); return; }
    if (event.target.closest("[data-net-apply]")) {
      const selected = $("[data-net-scope].primary");
      const name = selected && selected.dataset.netScope;
      if (!name) return toast("Choose a network scope first.");
      const confirmText = name === "lan-tailnet"
        ? "Bind the dashboard and the same API to this device's private LAN and Tailscale addresses, then restart the dashboard? Remote sign-in stays required."
        : name === "tailnet" ? "Bind the dashboard and the same API to Tailscale, then restart the dashboard?" : "Limit the dashboard and API to this device, then restart the dashboard?";
      if (!window.confirm(confirmText)) return;
      try {
        const r = await postJSON("/api/settings/network-scope", { scope: name });
        if (!r.ok) return toast(r.error || "The network scope could not be applied.");
        toast(r.restarting ? "Dashboard restarting with the selected network scope." : "Network scope saved.");
        await loadAccess();
      } catch (e) { toast("The dashboard may be restarting. Reconnect at its selected address."); }
      return;
    }
    const action = event.target.closest("[data-net-action]");
    if (action) {
      if (action.dataset.netAction === "https-on") {
        if (!window.confirm("Enable Tailscale Serve for private HTTPS access to this dashboard?")) return;
        startJob("https-on", {}, async (job) => { if (job && job.status === "ok") { await postJSON("/api/settings/public-refresh", {}); loadAccess(); } });
      } else if (action.dataset.netAction === "public-api") {
        if (!window.confirm("Make the model API reachable from the public internet through Tailscale Funnel? Every request still needs the shared API key. This is separate from private /api/v1 access.")) return;
        startJob("public-on", { what: "api" }, async (job) => { if (job && job.status === "ok") { await postJSON("/api/settings/public-refresh", {}); loadAccess(); } });
      }
      return;
    }
    const probe = event.target.closest("[data-setup='probe']");
    if (probe) {
      collectSetup(); D.error = ""; D.probe = null; D.verified = false; D.fingerprint = ""; D.busy = true; renderSetup();
      try {
        const r = await postJSON("/api/setup/node/probe", { host: D.host, port: D.port });
        if (!r.ok) throw new Error(r.error || "Couldn't check the SSH host key.");
        D.probe = r; D.host = r.host; D.port = String(r.port); D.fingerprint = r.suggested || (r.fingerprints[0] && r.fingerprints[0].fingerprint) || "";
      } catch (e) { D.error = e.message || "Couldn't check the SSH host key."; }
      D.busy = false; renderSetup(); return;
    }
    const add = event.target.closest("[data-setup='add']");
    if (add) {
      collectSetup(); D.error = "";
      if (!D.probe || D.probe.host !== D.host || String(D.probe.port) !== String(D.port)) { D.error = "Probe this host and port again before installing."; renderSetup(); return; }
      if (!/^[A-Za-z_][A-Za-z0-9._-]{0,31}$/.test(D.user)) { D.error = "Enter a valid SSH user name."; renderSetup(); return; }
      if (D.iface && !/^[A-Za-z0-9_.-]{1,15}$/.test(D.iface)) { D.error = "The interface name is not valid."; renderSetup(); return; }
      if (!D.verified) { D.error = "Compare the host fingerprint before continuing."; renderSetup(); return; }
      if (!D.probe.fingerprints.some((item) => item.fingerprint === D.fingerprint)) { D.error = "Choose a fingerprint from the verified probe result."; renderSetup(); return; }
      if (!window.confirm("Install Nodeyard on " + D.user + "@" + D.host + " and join it to this cluster as a worker? The chosen SSH fingerprint will be verified again before login.")) return;
      const args = ["worker", "--ssh", D.user + "@" + D.host, "--port", String(D.port), "--host-key", D.fingerprint];
      if (D.iface) args.push("--interface", D.iface);
      D.busy = true; D.job = "Starting…"; renderSetup();
      const started = await startJob("cli", { path: "add-node", args: args.join(" "), yes: true }, async (job) => {
        D.busy = false;
        D.job = job && job.id ? job.id : "finished";
        if (!job || job.status !== "ok") D.error = "The worker setup job did not finish successfully. Check its output in the job panel.";
        else toast("Worker setup completed.");
        renderSetup();
      });
      if (!started) { D.busy = false; D.job = ""; renderSetup(); }
      return;
    }
    const tour = event.target.closest("[data-tour]");
    if (tour) {
      if (tour.dataset.tour === "next") T.step = Math.min(TOUR.length - 1, T.step + 1);
      else if (tour.dataset.tour === "back") T.step = Math.max(0, T.step - 1);
      else if (tour.dataset.tour === "finish") T.step = 0;
      renderTour();
    }
  });

  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-setup-fp], [data-setup-verified]")) {
      collectSetup();
      if (event.target.matches("[data-setup-fp]")) D.fingerprint = event.target.value;
      if (event.target.matches("[data-setup-verified]")) D.verified = event.target.checked;
      renderSetup();
    }
  });
})();
